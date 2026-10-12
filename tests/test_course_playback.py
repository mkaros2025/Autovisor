import unittest
from unittest.mock import AsyncMock, patch

from modules.course_playback import (
    describe_media_state,
    duration_from_snapshot,
    learn_lesson,
    media_ready_for_lesson,
    media_state_is_new,
    media_state_is_playable,
    review_lesson,
    wait_for_lesson_media,
)
from modules.course_runner import CourseOutcome, run_course
from modules.lesson_navigation import WISDOM_CATALOG


def snapshot(
    duration=960.2,
    src="media-a",
    element=1,
    loads=1,
    meta=1,
    ready_state=4,
    has_video=True,
    current_time=0.0,
    ended=False,
):
    """一份 <video> 媒体状态快照(与 read_media_state 的返回结构一致)。"""
    return {
        "ok": True,
        "hasVideo": has_video,
        "id": element,
        "src": src,
        "duration": duration,
        "currentTime": current_time,
        "readyState": ready_state,
        "ended": ended,
        "loads": loads,
        "meta": meta,
    }


class _Config:
    limitMaxTime = 0


class _Logger:
    def __init__(self):
        self.events = []
        self.warnings = []

    def warn(self, message, **_kwargs):
        self.warnings.append(message)

    def info(self, *_args, **_kwargs):
        pass

    def context(self, **_kwargs):
        pass

    def error(self, *_args, **_kwargs):
        pass

    def section(self, *_args, **_kwargs):
        pass

    def debug(self, *_args, **_kwargs):
        pass

    def event(self, name, **fields):
        self.events.append((name, fields))

    def debug_throttled(self, *_args, **_kwargs):
        pass

    def log_exception(self, *_args, **_kwargs):
        pass

    @staticmethod
    def summarize_exception(exc):
        return type(exc).__name__


class _FakePage:
    def __init__(self):
        self.closed = False

    def is_closed(self):
        return self.closed

    async def wait_for_timeout(self, _ms):
        pass

    async def wait_for_selector(self, *_args, **_kwargs):
        return object()

    def set_default_timeout(self, _ms):
        pass


class _FakeLesson:
    def __init__(self):
        self.clicked = 0

    async def click(self, **_kwargs):
        self.clicked += 1

    def locator(self, _selector):
        raise AssertionError("本测试不应访问课时内部元素")


class _Event:
    def __init__(self):
        self.state = False

    def set(self):
        self.state = True

    def clear(self):
        self.state = False


class MediaGateTests(unittest.TestCase):
    """就绪判定必须只认"该 video 元素自身"的证据。"""

    def test_playable_requires_video_source_ready_state_and_duration(self):
        self.assertTrue(media_state_is_playable(snapshot()))
        self.assertFalse(media_state_is_playable(None))
        self.assertFalse(media_state_is_playable(snapshot(has_video=False)))
        self.assertFalse(media_state_is_playable(snapshot(src="")))
        self.assertFalse(media_state_is_playable(snapshot(ready_state=0)))
        self.assertFalse(media_state_is_playable(snapshot(duration=None)))
        self.assertFalse(media_state_is_playable(snapshot(duration=0)))
        self.assertFalse(media_state_is_playable(snapshot(duration=float("nan"))))

    def test_new_media_detection_uses_per_element_evidence(self):
        before = snapshot()
        self.assertFalse(media_state_is_new(snapshot(), before))
        self.assertTrue(media_state_is_new(snapshot(element=2, meta=1), before))
        self.assertTrue(media_state_is_new(snapshot(src="media-b"), before))
        self.assertTrue(media_state_is_new(snapshot(meta=2), before))

    def test_loadstart_on_other_elements_is_not_new_media_evidence(self):
        before = snapshot()
        # 同一元素、同一源、没有新的 loadedmetadata, 只是加载计数变化
        self.assertFalse(media_state_is_new(snapshot(loads=9, meta=1), before))

    def test_loadstart_without_loadedmetadata_keeps_old_media(self):
        before = snapshot()
        # 旧元素仅发生 loadstart: 仍可能保留旧的 currentSrc/duration, 不能放行
        self.assertFalse(
            media_ready_for_lesson(snapshot(loads=2, meta=1), before, False)
        )

    def test_snapshot_read_failure_is_not_new_media_evidence(self):
        self.assertFalse(media_state_is_new(snapshot(), None))
        self.assertFalse(media_ready_for_lesson(snapshot(), None, False))

    def test_first_lesson_without_any_video_is_accepted(self):
        empty = snapshot(has_video=False, src="", duration=None, element=None, meta=0)
        self.assertTrue(media_ready_for_lesson(snapshot(), empty, False))

    def test_ready_when_target_was_already_active_without_source_change(self):
        self.assertTrue(media_ready_for_lesson(snapshot(), snapshot(), True))

    def test_not_ready_when_switching_without_new_media(self):
        self.assertFalse(media_ready_for_lesson(snapshot(), snapshot(), False))

    def test_same_length_lessons_still_count_as_new_media(self):
        before = snapshot(duration=496.6, element=1, loads=1, meta=1)
        replaced = snapshot(duration=496.6, element=2, loads=1, meta=1)
        reloaded = snapshot(duration=496.6, element=1, loads=2, meta=2)
        self.assertTrue(media_ready_for_lesson(replaced, before, False))
        self.assertTrue(media_ready_for_lesson(reloaded, before, False))

    def test_describe_media_state_hides_media_url(self):
        text = describe_media_state(
            snapshot(src="https://cdn.example/video.mp4?token=secret-value")
        )
        self.assertNotIn("token", text)
        self.assertNotIn("http", text)
        self.assertNotIn("secret-value", text)

    def test_duration_is_only_refreshed_with_media_change_evidence(self):
        validated = snapshot(duration=960.2, element=1, meta=1)
        logger = _Logger()
        # 同一元素、无新的 loadedmetadata: 不接受任意后续 duration
        self.assertEqual(
            duration_from_snapshot(
                960.2, snapshot(duration=496.6, element=1, meta=1), validated, logger
            ),
            960.2,
        )
        # 同一元素发生新的 loadedmetadata: 接受新时长
        self.assertEqual(
            duration_from_snapshot(
                960.2, snapshot(duration=496.6, element=1, meta=2), validated, logger
            ),
            496.6,
        )
        # 元素被替换且已加载元数据: 接受新时长
        self.assertEqual(
            duration_from_snapshot(
                960.2, snapshot(duration=496.6, element=2, meta=1), validated, logger
            ),
            496.6,
        )
        # 缺少任一侧信息时不改动
        self.assertEqual(duration_from_snapshot(960.2, None, validated, logger), 960.2)
        self.assertEqual(
            duration_from_snapshot(960.2, snapshot(), None, logger), 960.2
        )
        self.assertEqual(
            duration_from_snapshot(
                960.2, snapshot(duration=None, meta=2), validated, logger
            ),
            960.2,
        )


class LessonEntryTests(unittest.IsolatedAsyncioTestCase):
    """播放入口必须使用确认过的时长, 而不是自行读取现存视频。"""

    async def test_learn_lesson_without_confirmed_duration_fails(self):
        result = await learn_lesson(
            _FakePage(), 0, 0, object(), WISDOM_CATALOG, _Config(), _Logger(), None
        )
        self.assertEqual(result, (0.0, False, False))

    async def test_review_lesson_without_confirmed_duration_fails(self):
        result = await review_lesson(
            _FakePage(), 0, 0, _Config(), _Logger(), None
        )
        self.assertEqual(result, (0.0, False, False))

    async def test_learn_lesson_uses_verified_duration(self):
        page = _FakePage()
        media = snapshot(duration=496.6, element=1, meta=1, current_time=10.0)
        with patch(
            "modules.course_playback.lesson_progress", new=AsyncMock(return_value=100)
        ), patch(
            "modules.course_playback.wait_for_lesson_completion",
            new=AsyncMock(return_value=True),
        ), patch(
            "modules.course_playback.read_media_state",
            new=AsyncMock(return_value=media),
        ):
            result = await learn_lesson(
                page,
                0,
                0,
                object(),
                WISDOM_CATALOG,
                _Config(),
                _Logger(),
                496.6,
                media,
            )
        self.assertEqual(result, (0.0, True, False))

    async def test_review_lesson_uses_verified_duration(self):
        page = _FakePage()
        media = snapshot(duration=496.6, element=1, meta=1, current_time=496.6)
        with patch(
            "modules.course_playback.read_media_state",
            new=AsyncMock(return_value=media),
        ), patch(
            "modules.course_playback.run_on", new=AsyncMock(return_value=None)
        ), patch(
            "modules.course_playback.has_visible_verification",
            new=AsyncMock(return_value=False),
        ), patch(
            "modules.course_playback.has_visible_element",
            new=AsyncMock(return_value=False),
        ):
            result = await review_lesson(
                page, 0, 0, _Config(), _Logger(), 496.6, media
            )
        self.assertEqual(result, (0.0, True, False))


class LessonMediaWaitTests(unittest.IsolatedAsyncioTestCase):
    async def _run_wait(self, states, before, target_was_active, timeout=1.0):
        page = _FakePage()
        with patch(
            "modules.course_playback.read_media_state",
            new=AsyncMock(side_effect=states),
        ), patch(
            "modules.course_playback.has_visible_verification",
            new=AsyncMock(return_value=False),
        ), patch(
            "modules.course_playback.has_visible_element",
            new=AsyncMock(return_value=False),
        ), patch(
            "modules.course_playback.MEDIA_POLL_INTERVAL_SECONDS", 0
        ):
            return await wait_for_lesson_media(
                page, before, target_was_active, _Logger(), timeout=timeout
            )

    async def test_waits_until_new_lesson_media_is_ready(self):
        before = snapshot(duration=960.2, src="media-a", element=1, meta=1)
        stale = snapshot(
            duration=960.2,
            src="media-a",
            element=1,
            loads=2,
            meta=1,
            current_time=497.0,
        )
        fresh = snapshot(duration=496.6, src="media-b", element=1, loads=3, meta=2)
        duration, paused, validated = await self._run_wait(
            [stale, stale, fresh], before, False
        )
        self.assertEqual(duration, 496.6)
        self.assertEqual(paused, 0.0)
        self.assertEqual(validated["src"], "media-b")

    async def test_times_out_without_new_media(self):
        before = snapshot(duration=960.2, src="media-a", element=1, meta=1)
        same = snapshot(duration=960.2, src="media-a", element=1, loads=5, meta=1)
        duration, _paused, validated = await self._run_wait(
            lambda *_: same, before, False, timeout=0.05
        )
        self.assertIsNone(duration)
        self.assertIsNone(validated)

    async def test_times_out_when_baseline_snapshot_failed(self):
        duration, _paused, validated = await self._run_wait(
            lambda *_: snapshot(), None, False, timeout=0.05
        )
        self.assertIsNone(duration)
        self.assertIsNone(validated)

    async def test_reuses_media_when_target_was_already_active(self):
        state = snapshot(duration=372.2, src="media-a", element=1, meta=1)
        duration, _paused, validated = await self._run_wait([state], state, True)
        self.assertEqual(duration, 372.2)
        self.assertEqual(validated["id"], 1)

    async def test_verification_popup_is_handled_during_the_wait(self):
        before = snapshot()
        fresh = snapshot(duration=496.6, src="media-b", loads=2, meta=2)
        page = _FakePage()
        hide = AsyncMock(return_value=None)
        with patch(
            "modules.course_playback.read_media_state",
            new=AsyncMock(side_effect=[fresh]),
        ), patch(
            "modules.course_playback.has_visible_verification",
            new=AsyncMock(side_effect=[True, False]),
        ), patch(
            "modules.course_playback.wait_until_verification_hidden", new=hide
        ), patch(
            "modules.course_playback.has_visible_element",
            new=AsyncMock(return_value=False),
        ), patch(
            "modules.course_playback.MEDIA_POLL_INTERVAL_SECONDS", 0
        ):
            duration, _paused, _validated = await wait_for_lesson_media(
                page, before, False, _Logger(), timeout=1.0
            )
        self.assertEqual(duration, 496.6)
        hide.assert_awaited()


class RunCourseIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """run_course 必须把确认过的时长与快照传给播放入口。"""

    def _patches(self, learn_or_review, filtered, media_result):
        lesson = _FakeLesson()
        return lesson, [
            patch(
                "modules.course_runner.get_filtered_class",
                new=AsyncMock(side_effect=filtered),
            ),
            patch(
                "modules.course_runner.resolve_lesson_click_target",
                new=AsyncMock(return_value=(lesson, None)),
            ),
            patch(
                "modules.course_runner.lesson_is_active",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "modules.course_runner.read_media_state",
                new=AsyncMock(return_value=snapshot(has_video=False)),
            ),
            patch(
                "modules.course_runner.close_blocking_modal",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "modules.course_runner.blocking_modal_visible",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "modules.course_runner.wait_for_lesson_active",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "modules.course_runner.get_lesson_name",
                new=AsyncMock(return_value="课时"),
            ),
            patch(
                "modules.course_runner.lesson_progress",
                new=AsyncMock(return_value=100),
            ),
            patch(
                "modules.course_runner.wait_for_lesson_media",
                new=AsyncMock(return_value=media_result),
            ),
            patch("modules.course_runner." + learn_or_review, new=AsyncMock(
                return_value=(0.0, True, False)
            )),
        ]

    async def test_learning_path_receives_verified_duration(self):
        validated = snapshot(duration=496.6, element=3, meta=2)
        lesson, patches = self._patches(
            "learn_lesson", [[object()]], (496.6, 1.5, validated)
        )
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        outcome = await run_course(
            _FakePage(), WISDOM_CATALOG, _Config(), _Logger(), _Event()
        )
        self.assertEqual(outcome, CourseOutcome.COMPLETED)
        import modules.course_runner as runner

        self.assertEqual(
            runner.learn_lesson.await_args.args[-2:], (496.6, validated)
        )

    async def test_review_path_receives_verified_duration(self):
        validated = snapshot(duration=372.2, element=1, meta=3)
        lesson = _FakeLesson()
        _, patches = self._patches(
            "review_lesson", [[], [lesson]], (372.2, 0.0, validated)
        )
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        outcome = await run_course(
            _FakePage(), WISDOM_CATALOG, _Config(), _Logger(), _Event()
        )
        self.assertEqual(outcome, CourseOutcome.COMPLETED)
        import modules.course_runner as runner

        self.assertEqual(
            runner.review_lesson.await_args.args[-2:], (372.2, validated)
        )

    async def test_unconfirmed_media_stops_the_run(self):
        lesson, patches = self._patches("learn_lesson", [[object()]], (None, 0.0, None))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        outcome = await run_course(
            _FakePage(), WISDOM_CATALOG, _Config(), _Logger(), _Event()
        )
        self.assertEqual(outcome, CourseOutcome.FAILED)


if __name__ == "__main__":
    unittest.main()

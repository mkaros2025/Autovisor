import asyncio
import unittest

from modules.lesson_navigation import (
    FUSION_CATALOG,
    HIKE_CATALOG,
    LEGACY_CATALOG,
    WISDOM_CATALOG,
    catalog_candidates,
    has_class,
    parse_progress_value,
    resolve_lesson_click_target,
)


class _FakeLocator:
    """只实现 resolve_lesson_click_target 用到的 count() / first。"""

    def __init__(self, count=0):
        self._count = count

    async def count(self):
        return self._count

    @property
    def first(self):
        return self


class _FakeLesson:
    """课时项容器: locator(内部选择器) 返回预设的课时行。"""

    def __init__(self, rows_count):
        self.rows = _FakeLocator(rows_count)

    def locator(self, selector):
        return self.rows


class LessonNavigationTests(unittest.TestCase):
    def test_current_wisdom_selectors_match_live_catalog(self):
        self.assertEqual(WISDOM_CATALOG.item, ".child-info.hasvideo")
        self.assertEqual(WISDOM_CATALOG.active, ".child-info.hasvideo.current")
        self.assertEqual(WISDOM_CATALOG.finish, ".child-check")
        self.assertEqual(WISDOM_CATALOG.progress_attribute, "aria-valuenow")

    def test_catalog_url_hints_only_change_detection_priority(self):
        self.assertIs(
            catalog_candidates("https://fusioncourseh5.zhihuishu.com/x")[0],
            FUSION_CATALOG,
        )
        self.assertEqual(
            catalog_candidates("https://hike.zhihuishu.com/x"),
            (HIKE_CATALOG,),
        )

    def test_progress_parser_clamps_and_rejects_non_finite_values(self):
        cases = {
            None: 0,
            "": 0,
            "69.9": 69,
            "100": 100,
            "101": 100,
            "-3": 0,
            "nan": 0,
            "inf": 0,
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(parse_progress_value(value), expected)

    def test_active_class_is_token_aware(self):
        self.assertTrue(has_class("child-info current cur hasvideo", "current"))
        self.assertFalse(has_class("child-info current-next", "current"))

    def test_fusion_click_target_is_the_lesson_row_not_the_container(self):
        # 真站实测: .chapter-item 中心落在小节标题与课时行之间的空隙上,
        # 点容器不切换课时(既不报错也不生效), 必须点 .chapter-content-second。
        self.assertEqual(FUSION_CATALOG.item, ".chapter-item")
        self.assertEqual(FUSION_CATALOG.click, ".chapter-content-second")

    def test_only_fusion_declares_a_click_target(self):
        for catalog in (WISDOM_CATALOG, LEGACY_CATALOG, HIKE_CATALOG):
            with self.subTest(catalog=catalog.name):
                self.assertIsNone(catalog.click)

    def test_click_target_is_the_single_inner_row(self):
        lesson = _FakeLesson(rows_count=1)
        target, error = asyncio.run(
            resolve_lesson_click_target(lesson, FUSION_CATALOG)
        )
        self.assertIsNone(error)
        self.assertIs(target, lesson.rows)

    def test_click_target_falls_back_to_container_when_row_is_absent(self):
        lesson = _FakeLesson(rows_count=0)
        target, error = asyncio.run(
            resolve_lesson_click_target(lesson, FUSION_CATALOG)
        )
        self.assertIsNone(error)
        self.assertIs(target, lesson)

    def test_click_target_rejects_ambiguous_rows(self):
        # 多个子行时不猜 .first, 宁可明确失败, 避免点错课时
        lesson = _FakeLesson(rows_count=3)
        target, error = asyncio.run(
            resolve_lesson_click_target(lesson, FUSION_CATALOG)
        )
        self.assertIsNone(target)
        self.assertIn("3", error)

    def test_catalogs_without_click_target_still_click_the_item(self):
        lesson = _FakeLesson(rows_count=1)
        for catalog in (WISDOM_CATALOG, LEGACY_CATALOG, HIKE_CATALOG):
            with self.subTest(catalog=catalog.name):
                target, error = asyncio.run(
                    resolve_lesson_click_target(lesson, catalog)
                )
                self.assertIsNone(error)
                self.assertIs(target, lesson)


if __name__ == "__main__":
    unittest.main()

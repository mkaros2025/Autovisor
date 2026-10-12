import asyncio
import math
import time

from playwright.async_api import Page, TimeoutError
from playwright._impl._errors import TargetClosedError

from modules.lesson_navigation import (
    CatalogSelectors,
    lesson_progress,
    wait_for_lesson_completion,
)
from modules.progress import show_course_progress
from modules.tasks import (
    has_visible_element,
    has_visible_verification,
    wait_until_verification_hidden,
)
from modules.utils import get_video_attr, run_on
from modules.fusion_adapter import FusionAdapter
from modules.video_state import (
    has_valid_duration,
    tail_retry_time,
    time_for_percent,
    video_at_end,
)

METADATA_TIMEOUT_SECONDS = 30
STALL_TIMEOUT_SECONDS = 120
# 等待"新课时的视频元数据"的上限。超时后明确失败, 不再回退接受可能属于上一课时的旧时长。
MEDIA_READY_TIMEOUT_SECONDS = 30.0
MEDIA_POLL_INTERVAL_SECONDS = 0.5

# 一次原子读取 <video> 的媒体状态:
# - 元素身份(id): 播放器可能换掉整个 video 元素;
# - src/duration/currentTime/readyState/ended: 同一快照内取值, 不混用不同媒体;
# - loads/meta: **按元素**统计的加载事件计数(loadstart / loadedmetadata),
#   在 document 捕获阶段监听以覆盖被替换的新元素。
#   仅 loadstart 不够——旧元素可能还留着旧的 currentSrc/duration; 只有该元素
#   自己发生了新的 loadedmetadata, 才能认为新媒体元数据已就绪。
MEDIA_STATE_JS = """() => {
  const w = window;
  if (!w.__autovisorMedia) {
    w.__autovisorMedia = {
      seq: 0, ids: new WeakMap(), loads: new WeakMap(), meta: new WeakMap()
    };
    const bump = (event) => {
      const el = event.target;
      if (!el || el.tagName !== 'VIDEO') return;
      const st = w.__autovisorMedia;
      st.loads.set(el, (st.loads.get(el) || 0) + 1);
      if (event.type === 'loadedmetadata') {
        st.meta.set(el, (st.meta.get(el) || 0) + 1);
      }
    };
    document.addEventListener('loadstart', bump, true);
    document.addEventListener('loadedmetadata', bump, true);
  }
  const state = w.__autovisorMedia;
  const video = document.querySelector('video');
  let id = null;
  let loads = 0;
  let meta = 0;
  if (video) {
    if (!state.ids.has(video)) state.ids.set(video, ++state.seq);
    id = state.ids.get(video);
    loads = state.loads.get(video) || 0;
    meta = state.meta.get(video) || 0;
  }
  return {
    ok: true,
    hasVideo: !!video,
    id: id,
    src: video ? (video.currentSrc || video.src || '') : '',
    duration: video ? video.duration : null,
    currentTime: video ? video.currentTime : null,
    readyState: video ? video.readyState : null,
    ended: video ? video.ended : null,
    loads: loads,
    meta: meta,
  };
}"""


def elapsed_minutes(start_time: float, paused_time: float) -> float:
    return max(0.0, time.time() - start_time - paused_time) / 60


async def _wait_for_topic_hidden(page: Page) -> float:
    wait_start = time.time()
    while await has_visible_element(page, (".topic-title",)):
        await asyncio.sleep(0.5)
    return time.time() - wait_start


async def read_media_state(page: Page, logger, quiet: bool = False):
    """原子读取当前 <video> 的媒体状态。

    读取失败返回 None —— 与"读取成功但页面上没有 video"(hasVideo=False) 区分开,
    因为只有前者需要视为"没有可比基准"而不能当作新媒体证据。
    """
    try:
        return await page.evaluate(MEDIA_STATE_JS)
    except TargetClosedError:
        raise
    except Exception as exc:
        if not quiet:
            logger.log_exception("读取视频媒体状态失败.", exc)
        return None


def describe_media_state(state) -> str:
    """日志用的脱敏描述: 不输出媒体 URL(可能携带签名令牌)。"""
    if not state:
        return "无"
    duration = state.get("duration")
    duration_text = (
        f"{duration:.1f}s"
        if isinstance(duration, (int, float)) and math.isfinite(duration)
        else "未知"
    )
    position = state.get("currentTime")
    position_text = (
        f"{position:.0f}s"
        if isinstance(position, (int, float)) and math.isfinite(position)
        else "未知"
    )
    return (
        f"元素={state.get('id')} 有源={bool(state.get('src'))} 时长={duration_text} "
        f"位置={position_text} 就绪={state.get('readyState')} "
        f"结束={state.get('ended')} 加载={state.get('loads')} 元数据={state.get('meta')}"
    )


def media_state_is_playable(state) -> bool:
    """有可用媒体: 存在 video 元素、已加载元数据(readyState>=1)且时长有效。"""
    if not state or not state.get("hasVideo"):
        return False
    if not state.get("src"):
        return False
    ready_state = state.get("readyState")
    if not isinstance(ready_state, int) or ready_state < 1:
        return False
    return has_valid_duration(state.get("duration"))


def media_state_is_new(state, before) -> bool:
    """state 是否已经是另一个课时的媒体。

    证据必须是该 video 元素自身的: 元素被替换 / 源改变 / **该元素**发生了新的
    loadedmetadata。仅 loadstart 不算(旧元素可能还留着旧的 currentSrc/duration);
    快照读取失败(before=None) 没有可比基准, 也不作为新媒体证据。
    """
    if before is None:
        return False
    if not before.get("hasVideo"):
        return True
    if state.get("id") != before.get("id"):
        return True
    if state.get("src") and state.get("src") != before.get("src"):
        return True
    return state.get("meta", 0) > before.get("meta", 0)


def media_ready_for_lesson(state, before, target_was_active: bool) -> bool:
    """是否可以拿 state 的时长作为本课时的时长。

    目标课时本来就是当前课时(未发生切换)时, 允许复用现有媒体, 不要求换源;
    真正切换到其他课时时, 必须同时满足"媒体可用"与"已是新课时的媒体"。
    """
    if not media_state_is_playable(state):
        return False
    if target_was_active:
        return True
    return media_state_is_new(state, before)


async def wait_for_lesson_media(
    page: Page, before, target_was_active: bool, logger, timeout: float | None = None
) -> tuple[float | None, float, dict | None]:
    """等待新课时的视频元数据就绪。

    返回 (时长, 等待期间累计暂停秒数, 通过校验的媒体快照)。
    读不到确认属于新课时的时长时返回 (None, 暂停秒数, None) —— 由调用方明确失败,
    不使用可能属于上一课时的旧时长继续播放。
    """
    limit = MEDIA_READY_TIMEOUT_SECONDS if timeout is None else timeout
    deadline = time.monotonic() + limit
    paused_time = 0.0
    last_state = None
    while time.monotonic() < deadline and not page.is_closed():
        if await has_visible_verification(page):
            wait_start = time.monotonic()
            await wait_until_verification_hidden(page)
            waited = time.monotonic() - wait_start
            deadline += waited
            paused_time += waited
            continue
        if await has_visible_element(page, (".topic-title",)):
            wait_start = time.monotonic()
            await _wait_for_topic_hidden(page)
            waited = time.monotonic() - wait_start
            deadline += waited
            paused_time += waited
            continue
        last_state = await read_media_state(page, logger)
        if media_ready_for_lesson(last_state, before, target_was_active):
            duration = float(last_state["duration"])
            logger.event(
                "视频元数据",
                时长=f"{duration:.1f}s",
                暂停=f"{paused_time:.1f}s",
                目标已激活=target_was_active,
                新媒体=media_state_is_new(last_state, before),
            )
            return duration, paused_time, last_state
        await asyncio.sleep(MEDIA_POLL_INTERVAL_SECONDS)
    logger.warn("等待新课视频元数据超时,停止当前课时.", shift=True)
    logger.event(
        "视频元数据",
        结果="超时",
        等待上限=f"{limit:.0f}s",
        目标已激活=target_was_active,
        切换前=describe_media_state(before),
        当前=describe_media_state(last_state),
    )
    return None, paused_time, None


def duration_from_snapshot(
    total_time: float | None, media, validated, logger
) -> float | None:
    """用**同一次原子快照**里的 duration 校正时长, 且必须有媒体已更新的证据。

    media 与 currentTime 来自同一个快照, 不会把不同媒体的时间混在一起;
    只有入场时校验过的快照出现新的 loadedmetadata(同一元素) 或元素被替换时,
    才接受这个新时长 —— 不对任意后续 duration 无条件覆盖。
    """
    if not media or validated is None:
        return total_time
    live = media.get("duration")
    if not has_valid_duration(live):
        return total_time
    same_element = media.get("id") == validated.get("id")
    new_metadata = media.get("meta", 0) > validated.get("meta", 0)
    replaced = not same_element and media.get("meta", 0) > 0
    if not (new_metadata or replaced):
        return total_time
    if not has_valid_duration(total_time) or abs(live - total_time) > 0.5:
        logger.event("时长刷新", 原时长=f"{total_time:.1f}s", 新时长=f"{live:.1f}s")
        return live
    return total_time


async def learn_lesson(
    page: Page,
    start_time: float,
    paused_before: float,
    current_lesson,
    catalog: CatalogSelectors,
    config,
    logger,
    total_time: float | None,
    validated_media=None,
) -> tuple[float, bool, bool]:
    # 时长由 run_course 确认"已是本课时的媒体"后传入; 这里不再自行读取,
    # 否则可能沿用上一课时残留的 <video> 元数据。
    paused_time = 0.0
    if not has_valid_duration(total_time):
        logger.warn("视频元数据不可用,停止当前课时.", shift=True)
        logger.event("视频元数据", 结果="不可用", 时长=f"{total_time}")
        return paused_time, False, False
    synced_to_catalog = False
    retry_count = 0
    last_catalog_progress = -1
    last_video_time = -1.0
    last_activity = time.monotonic()

    while True:
        try:
            if await has_visible_verification(page):
                wait_start = time.time()
                await wait_until_verification_hidden(page)
                paused_time += time.time() - wait_start
                logger.event(
                    "安全验证等待",
                    模块="学习进度",
                    耗时=f"{time.time() - wait_start:.1f}s",
                    平台进度=f"{last_catalog_progress}%",
                )
                synced_to_catalog = False
                retry_count = 0
                last_activity = time.monotonic()
                continue
            if await has_visible_element(page, (".topic-title",)):
                waited = await _wait_for_topic_hidden(page)
                paused_time += waited
                logger.event("课中弹题等待", 模块="学习进度", 耗时=f"{waited:.1f}s")
                retry_count = 0
                last_activity = time.monotonic()
                continue

            # 融合共享课: AI 随堂练习弹窗自动选作答+提交(官方 learn_lesson 未处理)。
            if catalog.name == "fusion" and await FusionAdapter.skip_ai_exercise(page):
                retry_count = 0
                last_activity = time.monotonic()
                continue

            if 0 < config.limitMaxTime <= elapsed_minutes(
                start_time, paused_before + paused_time
            ):
                return paused_time, False, True

            catalog_progress = await lesson_progress(current_lesson, catalog)
            if catalog_progress >= 100:
                await page.wait_for_timeout(2000)
                completed = await wait_for_lesson_completion(
                    current_lesson, catalog, timeout_ms=5000
                )
                logger.event(
                    "课时完成",
                    平台进度=f"{catalog_progress}%",
                    进度确认=completed,
                    暂停=f"{paused_time:.1f}s",
                )
                return paused_time, completed, False

            if catalog_progress > last_catalog_progress:
                last_catalog_progress = catalog_progress
                retry_count = 0
                last_activity = time.monotonic()

            # currentTime 与 duration 取自同一次原子快照, 避免混用不同媒体的数据
            media = await read_media_state(page, logger, quiet=True)
            current_time = media.get("currentTime") if media else None
            total_time = duration_from_snapshot(total_time, media, validated_media, logger)
            if isinstance(current_time, (int, float)) and current_time > last_video_time + 0.25:
                last_video_time = current_time
                last_activity = time.monotonic()
            logger.event(
                "播放状态",
                interval=60,
                已学=f"{elapsed_minutes(start_time, paused_before + paused_time):.1f}min",
                平台进度=f"{catalog_progress}%",
                播放器=(
                    f"{current_time:.0f}s"
                    if isinstance(current_time, (int, float))
                    else "未知"
                ),
                总时长=f"{total_time:.0f}s",
                重试次数=retry_count,
            )
            if time.monotonic() - last_activity >= STALL_TIMEOUT_SECONDS:
                logger.warn("视频和平台进度连续 120 秒未推进,停止当前课时.", shift=True)
                logger.event(
                    "视频停滞",
                    模块="学习进度",
                    播放器=(
                        f"{current_time:.0f}s"
                        if isinstance(current_time, (int, float))
                        else "未知"
                    ),
                    平台进度=f"{catalog_progress}%",
                    未推进秒=STALL_TIMEOUT_SECONDS,
                )
                return paused_time, False, False

            if has_valid_duration(total_time) and isinstance(
                current_time, (int, float)
            ):
                expected_time = time_for_percent(total_time, catalog_progress)
                if (
                    not synced_to_catalog
                    and catalog.progress
                    and current_time > expected_time + 15
                ):
                    logger.warn(
                        f"播放器进度快于平台记录,回到 {catalog_progress}% 继续.",
                        shift=True,
                    )
                    logger.event(
                        "进度对齐",
                        平台进度=f"{catalog_progress}%",
                        播放器=f"{current_time:.0f}s",
                        目标=f"{expected_time:.0f}s",
                    )
                    await page.evaluate(
                        """time => {
                            const video = document.querySelector('video');
                            if (!video) return;
                            video.currentTime = time;
                            video.play();
                        }""",
                        expected_time,
                    )
                    current_time = expected_time
                    last_video_time = expected_time
                    last_activity = time.monotonic()
                synced_to_catalog = True

                if video_at_end(current_time, total_time):
                    await page.wait_for_timeout(3000)
                    if await has_visible_verification(page):
                        wait_start = time.time()
                        await wait_until_verification_hidden(page)
                        paused_time += time.time() - wait_start
                        logger.event(
                            "安全验证等待",
                            模块="学习进度(视频结束)",
                            耗时=f"{time.time() - wait_start:.1f}s",
                        )
                        retry_count = 0
                        synced_to_catalog = False
                        last_activity = time.monotonic()
                        continue
                    if await has_visible_element(page, (".topic-title",)):
                        waited = await _wait_for_topic_hidden(page)
                        paused_time += waited
                        logger.event(
                            "课中弹题等待",
                            模块="学习进度(视频结束)",
                            耗时=f"{waited:.1f}s",
                        )
                        retry_count = 0
                        last_activity = time.monotonic()
                        continue
                    refreshed = await lesson_progress(current_lesson, catalog)
                    if refreshed >= 100:
                        logger.event(
                            "课时完成",
                            平台进度=f"{refreshed}%",
                            暂停=f"{paused_time:.1f}s",
                        )
                        return paused_time, True, False
                    if retry_count >= 2:
                        logger.warn(
                            f"视频已结束但平台进度停在 {refreshed}%,停止自动重试.",
                            shift=True,
                        )
                        logger.event(
                            "上报重试放弃",
                            平台进度=f"{refreshed}%",
                            重试次数=retry_count,
                        )
                        return paused_time, False, False
                    retry_time = (
                        time_for_percent(total_time, refreshed)
                        if catalog.progress
                        else tail_retry_time(total_time)
                    )
                    logger.warn(
                        f"视频已结束但平台仅记录 {refreshed}%,回退后重试上报.",
                        shift=True,
                    )
                    logger.event(
                        "上报重试",
                        平台进度=f"{refreshed}%",
                        目标时间=f"{retry_time:.1f}s",
                        第几次=retry_count + 1,
                    )
                    await page.evaluate(
                        """time => {
                            const video = document.querySelector('video');
                            if (!video) return;
                            video.currentTime = time;
                            video.play();
                        }""",
                        retry_time,
                    )
                    retry_count += 1
                    last_video_time = retry_time
                    last_activity = time.monotonic()
                    await asyncio.sleep(1)
                    continue

            show_course_progress(
                desc="平台记录进度:", cur_time=f"{catalog_progress}%"
            )
            await asyncio.sleep(0.5)
        except TargetClosedError:
            return paused_time, False, False
        except TimeoutError as exc:
            if await has_visible_verification(page):
                wait_start = time.time()
                await wait_until_verification_hidden(page)
                paused_time += time.time() - wait_start
            else:
                logger.debug_throttled(
                    "course_playback",
                    f"学习进度轮询未命中(元素 video): {logger.summarize_exception(exc)}",
                )


async def review_lesson(
    page: Page,
    start_time: float,
    paused_before: float,
    config,
    logger,
    total_time: float | None,
    validated_media=None,
) -> tuple[float, bool, bool]:
    # 同 learn_lesson: 时长由 run_course 确认后传入
    paused_time = 0.0
    if not has_valid_duration(total_time):
        logger.warn("视频元数据不可用,停止当前课时.", shift=True)
        logger.event("视频元数据", 结果="不可用", 时长=f"{total_time}")
        return paused_time, False, False
    try:
        await run_on(page, "video", "(el) => { el.currentTime = 0; }", "重置播放进度")
    except TargetClosedError:
        return paused_time, False, False
    last_video_time = -1.0
    last_activity = time.monotonic()

    while True:
        try:
            if await has_visible_verification(page):
                wait_start = time.time()
                await wait_until_verification_hidden(page)
                paused_time += time.time() - wait_start
                logger.event(
                    "安全验证等待",
                    模块="复习进度",
                    耗时=f"{time.time() - wait_start:.1f}s",
                )
                last_activity = time.monotonic()
                continue
            if await has_visible_element(page, (".topic-title",)):
                waited = await _wait_for_topic_hidden(page)
                paused_time += waited
                logger.event("课中弹题等待", 模块="复习进度", 耗时=f"{waited:.1f}s")
                last_activity = time.monotonic()
                continue

            # currentTime 与 duration 取自同一次原子快照, 避免混用不同媒体的数据
            media = await read_media_state(page, logger, quiet=True)
            current_time = media.get("currentTime") if media else None
            total_time = duration_from_snapshot(total_time, media, validated_media, logger)
            if video_at_end(current_time, total_time):
                logger.event(
                    "课时完成",
                    模块="复习",
                    播放器=f"{current_time:.0f}s",
                    暂停=f"{paused_time:.1f}s",
                )
                return paused_time, True, False
            if isinstance(current_time, (int, float)) and current_time > last_video_time + 0.25:
                last_video_time = current_time
                last_activity = time.monotonic()
            logger.event(
                "复习状态",
                interval=60,
                已学=f"{elapsed_minutes(start_time, paused_before + paused_time):.1f}min",
                播放器=(
                    f"{current_time:.0f}s"
                    if isinstance(current_time, (int, float))
                    else "未知"
                ),
                总时长=f"{total_time:.0f}s",
            )
            if time.monotonic() - last_activity >= STALL_TIMEOUT_SECONDS:
                logger.warn("视频连续 120 秒未推进,停止当前课时.", shift=True)
                logger.event(
                    "视频停滞",
                    模块="复习",
                    播放器=(
                        f"{current_time:.0f}s"
                        if isinstance(current_time, (int, float))
                        else "未知"
                    ),
                    未推进秒=STALL_TIMEOUT_SECONDS,
                )
                return paused_time, False, False
            if 0 < config.limitMaxTime <= elapsed_minutes(
                start_time, paused_before + paused_time
            ):
                return paused_time, False, True
            show_course_progress(
                desc="完成进度:",
                cur_time=elapsed_minutes(start_time, paused_before + paused_time),
                limit_time=config.limitMaxTime,
            )
            await asyncio.sleep(0.5)
        except TargetClosedError:
            return paused_time, False, False
        except TimeoutError as exc:
            if await has_visible_verification(page):
                wait_start = time.time()
                await wait_until_verification_hidden(page)
                paused_time += time.time() - wait_start
            else:
                logger.debug_throttled(
                    "review_lesson",
                    f"复习进度轮询未命中(元素 video): {logger.summarize_exception(exc)}",
                )

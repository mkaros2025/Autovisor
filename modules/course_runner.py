import time
from enum import Enum

from playwright.async_api import Page, TimeoutError

from modules.course_playback import (
    elapsed_minutes,
    learn_lesson,
    read_media_state,
    review_lesson,
    wait_for_lesson_media,
)
from modules.lesson_navigation import (
    CatalogSelectors,
    detect_catalog,
    lesson_is_active,
    lesson_progress,
    wait_for_lesson_active,
)
from modules.tasks import has_visible_verification, wait_until_verification_hidden
from modules.utils import (
    blocking_modal_visible,
    close_blocking_modal,
    get_filtered_class,
    get_lesson_name,
)
from modules.fusion_adapter import FusionAdapter

# 课时项(<li>)的点击发生在 set_default_timeout(10000) 之前, 不显式限时就会沿用
# 页面默认的 24 小时; 弹窗未关、被遮挡等异常情况下会一直卡在可操作性等待上。
LESSON_CLICK_TIMEOUT_MS = 10_000

# 目录节点在 detect_catalog 里刚确认过存在, 这里只做一次快速复核。
CATALOG_ATTACH_TIMEOUT_MS = 5_000


class CourseOutcome(Enum):
    COMPLETED = "completed"
    TIME_LIMIT = "time_limit"
    FAILED = "failed"


async def click_lesson_target(target, position: str, logger) -> bool:
    """点击课时行; 被遮罩拦截/超时时不抛异常, 返回 False 交给上层重试。

    真站上"学前必读/课程提醒"遮罩会在课程中途再次弹出, 直接点击会一直重试到
    10 秒超时并抛 TimeoutError; 不处理就会把整轮运行带崩。
    """
    try:
        await target.click(timeout=LESSON_CLICK_TIMEOUT_MS)
        return True
    except TimeoutError as exc:
        logger.warn("课时点击被拦截或超时.", shift=True)
        logger.event(
            "课时点击失败", 序号=position, 原因=logger.summarize_exception(exc)
        )
        return False


async def detect_catalog_after_verification(
    page: Page, course_url: str
) -> CatalogSelectors:
    deadline = time.monotonic() + 20
    while True:
        if await has_visible_verification(page):
            await wait_until_verification_hidden(page)
            deadline = time.monotonic() + 20
        remaining_ms = int(max(0, deadline - time.monotonic()) * 1000)
        if remaining_ms <= 0:
            raise RuntimeError("课程目录加载超时，且未检测到可处理的安全验证")
        try:
            return await detect_catalog(
                page, course_url, timeout_ms=min(1000, remaining_ms)
            )
        except RuntimeError:
            continue


async def run_course(
    page: Page,
    catalog: CatalogSelectors,
    config,
    logger,
    playback_enabled,
) -> CourseOutcome:
    await page.wait_for_selector(
        catalog.item, state="attached", timeout=CATALOG_ATTACH_TIMEOUT_MS
    )
    # 融合共享课: 默认折叠的章节其课时行是 display:none(Playwright 点不到),
    # 先展开全部章节, 否则课时切换会一直超时。
    if catalog.name == "fusion":
        await FusionAdapter.expand_catalog(page)
        await page.wait_for_timeout(400)
    to_learn = await get_filtered_class(page, catalog)
    learning = bool(to_learn)
    lessons = (
        to_learn
        if learning
        else await get_filtered_class(page, catalog, include_all=True)
    )
    logger.event(
        "目录筛选",
        模式="学习" if learning else "复习",
        待学课时=len(to_learn),
        本轮课时=len(lessons),
    )
    if not lessons:
        logger.error("课程目录中没有可播放的视频课时.")
        logger.event("课程结果", 结果="无可用课时")
        return CourseOutcome.FAILED

    start_time = time.time()
    paused_time = 0.0
    for index, lesson in enumerate(lessons):
        position = f"{index + 1}/{len(lessons)}"
        playback_enabled.clear()
        # 切换前先固定基准: 目标课时是否已激活 + 当前 <video> 的媒体状态。
        # 播放器会复用同一个 video 元素, 切换瞬间仍带着上一课时的 duration/currentTime,
        # 不记录基准就无法分辨"新课时媒体已加载"与"上一课时的残留元数据"。
        target_was_active = await lesson_is_active(lesson, catalog)
        media_before = await read_media_state(page, logger)
        if media_before is None:
            # 快照读取失败时没有可比基准, 再试一次; 仍失败则明确失败而不是拿旧时长凑数
            media_before = await read_media_state(page, logger)
        closed = await close_blocking_modal(page)
        if closed:
            logger.info(f"已关闭 {closed} 个拦截弹窗.")
            logger.event("拦截弹窗", 序号=position, 关闭数=closed)
        elif await blocking_modal_visible(page):
            logger.warn("存在无法关闭的遮罩弹窗,课时可能点不动.", shift=True)
            logger.event("拦截弹窗", 序号=position, 结果="无法关闭")
        clicked = await click_lesson_target(lesson, position, logger)
        active = clicked and await wait_for_lesson_active(lesson, catalog)
        if not active:
            logger.warn("课时切换超时,正在重试一次.", shift=True)
            logger.event(
                "课时激活超时",
                序号=position,
                目录类型=catalog.name,
                选择器=catalog.active,
            )
            # 首次点击可能只是把"学前必读"弹窗点出来: 关掉它再点一次才会真的切课时
            closed = await close_blocking_modal(page)
            if closed:
                logger.event("拦截弹窗", 序号=position, 关闭数=closed, 阶段="重试前")
            clicked = await click_lesson_target(lesson, position, logger)
            active = clicked and await wait_for_lesson_active(lesson, catalog)
        if not active:
            logger.error(f"无法选中课时,目录类型: {catalog.name}")
            logger.event("课程结果", 结果="课时无法选中", 序号=position)
            return CourseOutcome.FAILED

        await page.wait_for_timeout(1000)
        title = await get_lesson_name(page, lesson, catalog)
        logger.info(f"正在学习:{title}")
        initial_progress = await lesson_progress(lesson, catalog)
        logger.context(课时序号=position, 课时名称=title)
        logger.event(
            "课时开始",
            序号=position,
            标题=title,
            平台进度=f"{initial_progress}%",
        )
        lesson_start = time.time()
        page.set_default_timeout(10000)
        await page.wait_for_selector("video", state="attached")
        playback_enabled.set()

        total_time, media_paused, validated_media = await wait_for_lesson_media(
            page, media_before, target_was_active, logger
        )
        if total_time is None:
            logger.error("无法确认新课时的视频元数据,停止本轮运行.", shift=True)
            logger.event("课程结果", 结果="视频元数据未就绪", 序号=position)
            return CourseOutcome.FAILED
        paused_time += media_paused

        if learning:
            added_pause, completed, reached_limit = await learn_lesson(
                page,
                start_time,
                paused_time,
                lesson,
                catalog,
                config,
                logger,
                total_time,
                validated_media,
            )
        else:
            added_pause, completed, reached_limit = await review_lesson(
                page, start_time, paused_time, config, logger, total_time, validated_media
            )
        paused_time += added_pause

        final_progress = await lesson_progress(lesson, catalog)
        logger.event(
            "课时结果",
            序号=position,
            标题=title,
            完成=completed,
            达到时限=reached_limit,
            平台进度=f"{final_progress}%",
            本课用时=f"{time.time() - lesson_start:.0f}s",
        )
        if reached_limit:
            logger.info(f"当前课程已达时限:{config.limitMaxTime}min", shift=True)
            logger.info("即将进入下门课程!")
            logger.event("课程结果", 结果="达到时限", 课时数=len(lessons))
            return CourseOutcome.TIME_LIMIT
        if not completed:
            logger.warn(
                f'"{title}" 未能确认播放完成,本轮停止切换下一课.',
                shift=True,
            )
            logger.event(
                "课程结果",
                结果="课时未确认完成",
                序号=position,
                平台进度=f"{final_progress}%",
            )
            return CourseOutcome.FAILED

        if index < len(lessons) - 1:
            logger.info(f'"{title}" 已完成!', shift=True)
            logger.info(
                f"本次课程已学习:{elapsed_minutes(start_time, paused_time):.1f} min"
            )

    if learning:
        logger.info("已学完本课程全部内容!", shift=True)
        print("==" * 10)
    else:
        logger.info(f'"{title}" 已完成!', shift=True)
    logger.event("课程结果", 结果="全部完成", 课时数=len(lessons))
    return CourseOutcome.COMPLETED

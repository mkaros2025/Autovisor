import asyncio
import math
from dataclasses import dataclass

from playwright.async_api import Locator, Page, TimeoutError


@dataclass(frozen=True)
class CatalogSelectors:
    name: str
    item: str
    active: str | None
    finish: str
    title: str
    active_class: str
    progress: str | None = None
    progress_attribute: str | None = None
    course_title: str | None = None
    # item 内部真正的"课时行"。融合课的 item(.chapter-item) 是**小节**, 一个小节可以挂
    # 1~N 个课时行(.chapter-content-second, 如 4.3 有 4 个、6.4 有 2 个); 声明该字段后
    # 采集课时会先把每个 item 展开成它的课时行, 而没有行的小节(平台数据
    # ishaveChildrenLesson==0, 小节自身就是课时)仍以 item 本身作为课时。
    # 其余目录一个 item 就是一个课时, 保持 None。
    lesson_row: str | None = None


WISDOM_CATALOG = CatalogSelectors(
    name="wisdom",
    item=".child-info.hasvideo",
    active=".child-info.hasvideo.current",
    finish=".child-check",
    title=".child-name",
    active_class="current",
    progress="[role='progressbar'][aria-valuenow]",
    progress_attribute="aria-valuenow",
    course_title=".course-name",
)

FUSION_CATALOG = CatalogSelectors(
    name="fusion",
    # 目录锚点仍用 .chapter-item(小节): 它一定存在, 与章节展开状态无关。
    item=".chapter-item",
    # 真站实测(render 函数 + DOM 快照): .chapter-item 是**小节**,
    # 其下的 .chapter-content-second 才是**课时行**——小节标题行显示 x.y,
    # 课时行显示 x.y.z。一个小节可以挂多个课时行(4.3 培养心理韧性 4 个,
    # 6.4 功夫在诗外 2 个); 平台自己的"未完成视频"统计也是
    # "有子课时就看子课时, 没有才看小节"。故课时单位必须取课时行,
    # 否则多课时小节点不动、也会漏学(小节级 finish-icon 只反映"小节的某个子课时" 已完成)。
    lesson_row=".chapter-content-second",
    # current 直接挂在小节/课时行自身(小节: class="chapter-item current",
    # 课时行: class="chapter-content-second current"), 无需再找内部后代。
    active=None,
    finish=".finish-icon",
    title=".item-name",
    active_class="current",
    # 进度环 .el-progress 就在课时行(或无可展开子课时的小节)内部,
    # aria-valuenow 给出该课时真实的已学百分比(缓增, 而非只到 0/100),
    # 支撑"播完未到 100 -> 回退重播"的上报。
    progress=".el-progress",
    progress_attribute="aria-valuenow",
)

HIKE_CATALOG = CatalogSelectors(
    name="hike",
    item=".file-item",
    active=".file-item.active",
    finish=".icon-finish",
    title="span[title]",
    active_class="active",
    progress=".rate",
    course_title=".course-name",
)

LEGACY_CATALOG = CatalogSelectors(
    name="legacy",
    item=".clearfix.video",
    active=".clearfix.video.current_play",
    finish=".time_icofinish",
    title="#lessonOrder",
    active_class="current_play",
    progress=".progress-num",
    course_title=".source-name",
)

CATALOGS = (WISDOM_CATALOG, FUSION_CATALOG, HIKE_CATALOG, LEGACY_CATALOG)


def parse_progress_value(value) -> int:
    if isinstance(value, str):
        value = value.strip().removesuffix("%")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    if not math.isfinite(number):
        return 0
    return max(0, min(int(number), 100))


def has_class(class_attr: str | None, class_name: str) -> bool:
    return class_name in (class_attr or "").split()


def catalog_candidates(course_url: str) -> tuple[CatalogSelectors, ...]:
    if "hike.zhihuishu.com" in course_url:
        return (HIKE_CATALOG,)
    if "fusioncourseh5" in course_url:
        return (FUSION_CATALOG, WISDOM_CATALOG, LEGACY_CATALOG)
    return (WISDOM_CATALOG, LEGACY_CATALOG, FUSION_CATALOG)


async def detect_catalog(
    page: Page, course_url: str = "", timeout_ms: int = 20_000
) -> CatalogSelectors:
    candidates = catalog_candidates(course_url or page.url)
    selector = ", ".join(catalog.item for catalog in candidates)
    try:
        await page.wait_for_selector(selector, state="attached", timeout=timeout_ms)
    except TimeoutError as exc:
        raise RuntimeError(
            f"课程目录加载超时, 未识别到新版/旧版/翻转课目录(尝试的选择器: {selector})"
        ) from exc

    for catalog in candidates:
        if await page.locator(catalog.item).count() > 0:
            return catalog
    raise RuntimeError(
        f"课程目录结构无法识别(已存在目录节点但未匹配: {selector})"
    )


async def iter_lessons(page: Page, catalog: CatalogSelectors) -> list[Locator]:
    """按目录顺序采集可播课时单元。

    catalog.lesson_row 为 None 时 item 就是课时; 否则把每个 item 展开成它的课时行
    (一个小节可能挂多个课时), 而没有课时行的小节自身就是课时
    (真站数据 ishaveChildrenLesson==0, 渲染成 .item-box 一行, 点它就是播它自己的视频)。
    """
    items = await page.locator(catalog.item).all()
    if not catalog.lesson_row:
        return items

    lessons: list[Locator] = []
    for item in items:
        rows = await item.locator(catalog.lesson_row).all()
        lessons.extend(rows or [item])
    return lessons


def lesson_unit_selector(catalog: CatalogSelectors) -> str:
    """当前激活的课时单元选择器。

    课时单元可能是 item 内的课时行(融合课), 也可能是 item 自身, 两者都把 active_class
    挂在自己身上, 这里统一成一个选择器供需要"当前课时"的调用方使用。
    """
    class_selector = f".{catalog.active_class}"
    selectors = [f"{catalog.item}{class_selector}"]
    if catalog.lesson_row:
        selectors.insert(0, f"{catalog.lesson_row}{class_selector}")
    return ", ".join(selectors)


async def lesson_progress(lesson: Locator, catalog: CatalogSelectors) -> int:
    if await lesson.locator(catalog.finish).count() > 0:
        return 100
    if not catalog.progress:
        return 0

    progress = lesson.locator(catalog.progress).first
    if await progress.count() == 0:
        return 0
    if catalog.progress_attribute:
        value = await progress.get_attribute(catalog.progress_attribute)
    else:
        value = await progress.text_content()
    return parse_progress_value(value)


async def lesson_is_complete(lesson: Locator, catalog: CatalogSelectors) -> bool:
    return await lesson_progress(lesson, catalog) >= 100


async def wait_for_lesson_completion(
    lesson: Locator, catalog: CatalogSelectors, timeout_ms: int = 5_000
) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
    while asyncio.get_running_loop().time() < deadline:
        if await lesson_is_complete(lesson, catalog):
            return True
        await asyncio.sleep(0.25)
    return False


async def lesson_is_active(lesson: Locator, catalog: CatalogSelectors) -> bool:
    """课时当前是否处于激活状态。

    "当前激活"在课时单元自身(class="... current")上; 个别目录结构(如旧版)还会把标识
    挂在课时内部子节点上, 因此本节点没命中时再回退查内部是否命中 catalog.active。
    """
    try:
        if has_class(await lesson.get_attribute("class"), catalog.active_class):
            return True
    except Exception:
        return False
    if not catalog.active:
        return False
    try:
        return await lesson.locator(catalog.active).count() > 0
    except Exception:
        return False


async def wait_for_lesson_active(
    lesson: Locator, catalog: CatalogSelectors, timeout_ms: int = 8_000
) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
    while asyncio.get_running_loop().time() < deadline:
        if await lesson_is_active(lesson, catalog):
            return True
        await asyncio.sleep(0.2)
    return False


async def get_lesson_title(
    page: Page, lesson: Locator, catalog: CatalogSelectors
) -> str:
    scoped = lesson.locator(catalog.title).first
    title_element = scoped if await scoped.count() else page.locator(catalog.title).first
    if await title_element.count():
        title = await title_element.get_attribute("title")
        if title:
            return title.strip()
        text = await title_element.text_content()
        if text:
            return " ".join(text.split())

    text = await lesson.text_content()
    return " ".join((text or "当前课时").split())

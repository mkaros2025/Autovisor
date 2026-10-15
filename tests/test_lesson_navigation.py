import asyncio
import unittest

from modules.lesson_navigation import (
    FUSION_CATALOG,
    HIKE_CATALOG,
    LEGACY_CATALOG,
    WISDOM_CATALOG,
    catalog_candidates,
    has_class,
    iter_lessons,
    lesson_is_active,
    lesson_unit_selector,
    parse_progress_value,
)


class _FakeLocator:
    """只实现 iter_lessons / lesson_is_active 用到的接口。"""

    def __init__(self, rows=None, class_attr=None):
        self._rows = list(rows or [])
        self._class_attr = class_attr

    async def all(self):
        return list(self._rows)

    async def get_attribute(self, name):
        return self._class_attr if name == "class" else None

    async def count(self):
        return len(self._rows)


class _FakeItem(_FakeLocator):
    """小节/课时项: locator(内部选择器) 返回它自己的课时行。"""

    def locator(self, selector):
        return _FakeLocator(self._rows)


class _FakePage:
    def __init__(self, items):
        self._items = list(items)

    def locator(self, selector):
        return _FakeLocator(self._items)


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

    def test_fusion_lessons_are_the_rows_inside_the_section(self):
        # 真站(render 函数 + DOM 快照)实测: .chapter-item 是"小节", 其下的
        # .chapter-content-second 才是课时行(x.y.z); 一个小节可挂多个课时
        # (4.3 培养心理韧性 4 个, 6.4 功夫在诗外 2 个), 不含行的小节自身即课时。
        self.assertEqual(FUSION_CATALOG.item, ".chapter-item")
        self.assertEqual(FUSION_CATALOG.lesson_row, ".chapter-content-second")
        # current 挂在课时单元自身, 不需要内部后代回退
        self.assertIsNone(FUSION_CATALOG.active)

    def test_only_fusion_declares_inner_lesson_rows(self):
        for catalog in (WISDOM_CATALOG, LEGACY_CATALOG, HIKE_CATALOG):
            with self.subTest(catalog=catalog.name):
                self.assertIsNone(catalog.lesson_row)

    def test_iter_lessons_expands_rows_and_keeps_rowless_sections(self):
        rowless = _FakeItem(rows=[])
        multi = _FakeItem(rows=["4.3.1", "4.3.2", "4.3.3", "4.3.4"])
        single = _FakeItem(rows=["7.1.1"])
        page = _FakePage([rowless, multi, single])

        lessons = asyncio.run(iter_lessons(page, FUSION_CATALOG))

        self.assertEqual(
            lessons, [rowless, "4.3.1", "4.3.2", "4.3.3", "4.3.4", "7.1.1"]
        )

    def test_iter_lessons_returns_items_for_single_unit_catalogs(self):
        items = [_FakeItem(rows=["x"]), _FakeItem(rows=[])]
        page = _FakePage(items)

        for catalog in (WISDOM_CATALOG, LEGACY_CATALOG, HIKE_CATALOG):
            with self.subTest(catalog=catalog.name):
                self.assertEqual(asyncio.run(iter_lessons(page, catalog)), items)

    def test_lesson_is_active_reads_the_unit_class(self):
        row = _FakeItem(rows=[], class_attr="chapter-content-second current")
        idle = _FakeItem(rows=[], class_attr="chapter-content-second")

        self.assertTrue(asyncio.run(lesson_is_active(row, FUSION_CATALOG)))
        self.assertFalse(asyncio.run(lesson_is_active(idle, FUSION_CATALOG)))

    def test_active_unit_selector_covers_the_unit_for_every_catalog(self):
        self.assertEqual(
            lesson_unit_selector(FUSION_CATALOG),
            ".chapter-content-second.current, .chapter-item.current",
        )
        self.assertEqual(
            lesson_unit_selector(WISDOM_CATALOG), ".child-info.hasvideo.current"
        )
        self.assertEqual(lesson_unit_selector(HIKE_CATALOG), ".file-item.active")
        self.assertEqual(
            lesson_unit_selector(LEGACY_CATALOG), ".clearfix.video.current_play"
        )


if __name__ == "__main__":
    unittest.main()

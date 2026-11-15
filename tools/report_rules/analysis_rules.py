"""**vendored**: skill `build-amazon-integrated-html-report` 的确定性数值边界。

来源: `C:\\Users\\Jack_New\\.codex\\skills\\build-amazon-integrated-html-report\\scripts\\analysis_rules.py`
同步时间: 2026-10-12 | 源文件 sha256: 2e7006e6d867f018105d36a2e013cf4d7c35997b27491fa0cda311eb942ec301
自检: `python tools/report_rules/analysis_rules.py --self-test`(11 项边界测试)

为什么不直接 import 用户目录下的技能文件: 报告生成必须在**本仓库内自包含**(看板/CI 不依赖用户 profile 路径)。
重新同步: 复制源文件并把上面这行 sha256 更新; `tests/test_report_rules.py` 会校验与源文件的一致性(源不存在时跳过)。
"""
import argparse
import math
import unittest


def number(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError('Use numeric values or None; parse source units explicitly')
    if not math.isfinite(value):
        raise ValueError('Non-finite value')
    return float(value)


def relative_change(target, baseline, comparable=True):
    target, baseline = number(target), number(baseline)
    if not comparable or target is None or baseline is None or baseline <= 0:
        return None
    return target / baseline - 1


def keyword_admission(volume, amazon_yoy, google_yoy, cpc):
    rules = (
        ('Amazon月搜索量', number(volume), lambda x: x >= 30000),
        ('Amazon同比', number(amazon_yoy), lambda x: x >= -0.20),
        ('Google同比', number(google_yoy), lambda x: x >= -0.20),
        ('Amazon CPC（USD）', number(cpc), lambda x: x < 3.00),
    )
    # Negative volume/CPC is invalid, not a passing cheap price.
    if rules[0][1] is not None and rules[0][1] < 0:
        raise ValueError('Negative volume')
    if rules[3][1] is not None and rules[3][1] < 0:
        raise ValueError('Negative CPC')
    failed = [label for label, value, predicate in rules
              if value is not None and not predicate(value)]
    missing = [label for label, value, _ in rules if value is None]
    status = '不达标' if failed else '数据不足' if missing else '达标'
    return {'status': status, 'failed': failed, 'missing': missing}


def dual_rank_status(gap_pp):
    value = number(gap_pp)
    if value is None:
        return '数据不足'
    return '新品增强' if value >= 8 else '成熟榜更强' if value <= -8 else '两榜接近'


def mention_frequency(share):
    value = number(share)
    if value is None:
        return None
    if not 0 <= value <= 1:
        raise ValueError('Unique-review mention share must be between 0 and 1')
    return 5 if value >= .20 else 4 if value >= .10 else 3 if value >= .05 else 2 if value >= .02 else 1


def recommendation_strength(share):
    value = number(share)
    if value is None:
        return None
    if not 0 <= value <= 1:
        raise ValueError('Recommendation share must be between 0 and 1')
    return 5 if value >= .20 else 4 if value >= .10 else 3 if value >= .05 else 2 if value > 0 else 1


def trend_component(recent_share, earlier_share, recent_n, earlier_n, coverage_days):
    recent, earlier = number(recent_share), number(earlier_share)
    if recent is None or earlier is None or recent_n < 30 or earlier_n < 30 or coverage_days < 120:
        return {'T': 3, 'status': '趋势数据不足', 'delta_pp': None}
    if not 0 <= recent <= 1 or not 0 <= earlier <= 1:
        raise ValueError('Trend shares must be review-share ratios')
    delta = (recent - earlier) * 100
    # Tolerance only for numeric subtraction at exact pp boundaries, not display rounding.
    ge = lambda boundary: delta >= boundary or math.isclose(delta, boundary, abs_tol=1e-10)
    le = lambda boundary: delta <= boundary or math.isclose(delta, boundary, abs_tol=1e-10)
    tier = 5 if ge(10) else 4 if ge(5) else 1 if le(-10) else 2 if le(-5) else 3
    return {'T': tier, 'status': '可计算', 'delta_pp': delta}


def score_components(values, weights):
    parsed = [number(value) for value in values]
    if any(value is None for value in parsed):
        return None
    if any(not 1 <= value <= 5 for value in parsed):
        raise ValueError('VOC component score must be on the 1–5 scale')
    return 20 * sum(value * weight for value, weight in zip(parsed, weights))


def positive_driver_score(P, B, R, V, C):
    score = score_components([P, B, R, V, C], [.30, .20, .20, .20, .10])
    if score is None:
        return {'score': None, 'tier': '数据不足'}
    tier = 'Core differentiator' if score >= 80 else 'Strong value driver' if score >= 65 else 'Supporting value' if score >= 50 else 'Emerging/low-salience'
    return {'score': score, 'tier': tier}


def negative_priority_score(F, S, H, T, C, credible_grade=None):
    if credible_grade not in (None, 'A', 'B', 'C', 'D'):
        raise ValueError('Unknown safety grade')
    expected_h = 5 if credible_grade in ('A', 'B') else 3 if credible_grade == 'C' else 1 if credible_grade == 'D' else None
    if expected_h is not None and H is not None and number(H) != expected_h:
        raise ValueError('H conflicts with credible safety grade')
    score = score_components([F, S, H, T, C], [.25, .30, .25, .10, .10])
    if score is None:
        return {'score': None, 'priority': '数据不足'}
    priority = 'P0' if score >= 80 else 'P1' if score >= 65 else 'P2' if score >= 50 else 'P3'
    if credible_grade in ('A', 'B') and priority in ('P2', 'P3'):
        priority = 'P1'
    # Grade D contributes H=1; any high score needs non-hearsay F/S/T/C support.
    return {'score': score, 'priority': priority}


def seasonality_gate(annual_composites, completed_through_year, shared_comparison=True):
    """Input: year -> 12 actual monthly shared-scale category composite values.

    Caller supplies country/search/category/normalization/source records and verifies
    month coverage. None is missing, never a zero fill. Latest three complete years.
    """
    insufficient = lambda reason: {'status': '季节性数据不足', 'reason': reason}
    if not shared_comparison:
        return insufficient('Google terms were not obtained in one shared normalization context')
    normalized = {}
    for year, values in annual_composites.items():
        if year > completed_through_year:
            continue
        if len(values) != 12:
            continue
        parsed = [number(value) for value in values]
        if any(value is None for value in parsed):
            continue
        if any(value < 0 for value in parsed):
            raise ValueError('Negative Google index')
        annual_mean = sum(parsed) / 12
        if annual_mean <= 0:
            continue
        normalized[year] = [value / annual_mean for value in parsed]
    years = sorted(normalized)[-3:]
    if len(years) < 2:
        return insufficient('Fewer than two usable complete calendar years')
    indices = [sum(normalized[year][month] for year in years) / len(years) for month in range(12)]
    peak = sorted(range(12), key=lambda month: (-indices[month], month))[:3]
    off = sorted(range(12), key=lambda month: (indices[month], month))[:3]
    peak_mean, off_mean = sum(indices[m] for m in peak) / 3, sum(indices[m] for m in off) / 3
    if off_mean <= 0:
        return insufficient('Zero off-season denominator; no finite peak/off ratio')
    ratio = peak_mean / off_mean
    repeat_peak = sum(sum(normalized[year][month] > 1.10 for year in years) >= 2 for month in peak)
    repeat_off = sum(sum(normalized[year][month] < .90 for year in years) >= 2 for month in off)
    strength = ratio >= 1.20 or math.isclose(ratio, 1.20, abs_tol=1e-12)
    passed = strength and repeat_peak >= 2 and repeat_off >= 2
    return {'status': '季节性成立' if passed else '季节性不成立', 'years': years,
            'indices': indices, 'peak_months': [m + 1 for m in peak],
            'off_months': [m + 1 for m in off], 'peak_off_ratio': ratio,
            'repeat_peak_count': repeat_peak, 'repeat_off_count': repeat_off}


class BoundaryTests(unittest.TestCase):
    def test_keyword_equalities(self):
        self.assertEqual(keyword_admission(30000, -.20, -.20, 2.99)['status'], '达标')
        self.assertEqual(keyword_admission(30000, -.20, -.20, 3)['status'], '不达标')

    def test_keyword_missing_and_fail_precedence(self):
        self.assertEqual(keyword_admission(30000, None, -.20, 2)['status'], '数据不足')
        result = keyword_admission(29999, None, -.21, 3)
        self.assertEqual(result['status'], '不达标')
        self.assertEqual(len(result['failed']), 3)
        self.assertEqual(len(result['missing']), 1)
        with self.assertRaises(ValueError):
            keyword_admission(30000, 0, 0, -1)

    def test_relative_changes(self):
        self.assertIsNone(relative_change(10, 0))
        self.assertIsNone(relative_change(10, None))
        self.assertIsNone(relative_change(None, 10))
        self.assertIsNone(relative_change(10, 5, False))
        self.assertEqual(relative_change(0, 10), -1)
        self.assertEqual(relative_change(20, 10), 1)

    def test_dual_rank(self):
        for value, expected in [(8, '新品增强'), (-8, '成熟榜更强'), (7.99, '两榜接近'), (None, '数据不足')]:
            self.assertEqual(dual_rank_status(value), expected)

    def test_frequency_and_recommendation(self):
        for value, expected in [(0, 1), (.02, 2), (.05, 3), (.10, 4), (.20, 5)]:
            self.assertEqual(mention_frequency(value), expected)
        self.assertEqual(recommendation_strength(0), 1)
        self.assertEqual(recommendation_strength(.01), 2)
        self.assertIsNone(mention_frequency(None))

    def test_trend_boundaries_and_coverage(self):
        for recent, expected in [(.20, 5), (.15, 4), (.10, 3), (.05, 2), (0, 1)]:
            self.assertEqual(trend_component(recent, .10, 30, 30, 120)['T'], expected)
        result = trend_component(.90, .10, 29, 100, 365)
        self.assertEqual(result['status'], '趋势数据不足')
        self.assertEqual(result['T'], 3)
        self.assertEqual(trend_component(.9, .1, 100, 100, 119)['status'], '趋势数据不足')

    def test_positive_score(self):
        self.assertAlmostEqual(positive_driver_score(5, 5, 5, 5, 5)['score'], 100)
        self.assertAlmostEqual(positive_driver_score(1, 1, 1, 1, 1)['score'], 20)
        self.assertEqual(positive_driver_score(4, 4, 4, 4, 4)['tier'], 'Core differentiator')
        self.assertIsNone(positive_driver_score(1, 1, None, 1, 1)['score'])

    def test_negative_score_and_safety_floor(self):
        self.assertEqual(negative_priority_score(1, 1, 1, 1, 1, 'D')['priority'], 'P3')
        self.assertEqual(negative_priority_score(1, 1, 5, 1, 1, 'A')['priority'], 'P1')
        self.assertEqual(negative_priority_score(5, 5, 5, 5, 5, 'B')['priority'], 'P0')
        with self.assertRaises(ValueError):
            negative_priority_score(1, 1, 5, 1, 1, 'D')

    def test_seasonality_pass(self):
        pattern = [1.6] * 3 + [1] * 6 + [.6] * 3
        result = seasonality_gate({2024: pattern, 2025: pattern}, 2025)
        self.assertEqual(result['status'], '季节性成立')
        self.assertEqual(result['repeat_peak_count'], 3)
        self.assertEqual(result['repeat_off_count'], 3)

    def test_flat_and_spike_not_seasonal(self):
        flat = [1] * 12
        self.assertEqual(seasonality_gate({2024: flat, 2025: flat}, 2025)['status'], '季节性不成立')
        spike = [10] + [1] * 11
        self.assertEqual(seasonality_gate({2024: spike, 2025: flat}, 2025)['status'], '季节性不成立')

    def test_seasonality_insufficient_and_missing(self):
        flat = [1] * 12
        cases = [({2025: flat}, True), ({2024: flat, 2025: flat}, False),
                 ({2024: flat, 2025: [None] + flat[1:]}, True),
                 ({2025: flat, 2026: flat}, True)]
        for rows, shared in cases:
            self.assertEqual(seasonality_gate(rows, 2025, shared)['status'], '季节性数据不足')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    if args.self_test:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(BoundaryTests)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        raise SystemExit(0 if result.wasSuccessful() else 1)
    parser.print_help()

import pytest
from tools.attr_stability.composite_length import convert_composite_cm_inches
from tools.attr_stability.step18_workspace import round_interval_value


@pytest.mark.parametrize(('raw', 'expected'), [
    ('15.34cm、12cm', '6.04inch、4.72inch'),
    ('35.56x26.42 cm', '14.00x10.40 inch'),
    ('16.002，13.97cm', '6.30，5.50inch'),
    ('缺x缺x10.01 cm', '缺x缺x3.94 inch'),
    ('64oz:20.32cm、40oz:19.05cm', '64oz:8.00inch、40oz:7.50inch'),
    ('小号:9.9cm、中-大号:10.9cm', '小号:3.90inch、中-大号:4.29inch'),
    ('饮水碗15.5cm、慢食碗16.5cm', '饮水碗6.10inch、慢食碗6.50inch'),
    ('固定10.16cm', '4.00inch'),
    ('约39.37cm', '15.50inch'),
    ('12.5cm或13.5cm', '4.92inch或5.31inch'),
    ('8cm,12cm,18cm,24cm,30cm', '3.15inch,4.72inch,7.09inch,9.45inch,11.81inch'),
    ('5.8–19.1cm，6.9–21.1cm，7.4–27.9cm', '2.28–7.52inch，2.72–8.31inch，2.91–10.98inch'),
    ('10.8-31.5 cm（4档可调）', '4.25-12.40 inch（4档可调）'),
    ('可调，最高30cm（6档，每档2cm）', '可调，最高11.81inch（6档，每档0.79inch）'),
    ("可调3.9''-11.8''（约10-30cm）", "可调3.90''-11.80''（3.90-11.80inch）"),
    ('可调5-16英寸（约12.7-40.6cm）', '可调5.00-16.00英寸（5.00-16.00inch）'),
    ('17.5cm（玻璃碗6.9英寸）、17cm（不锈钢碗6.7英寸）', '6.90inch（玻璃碗6.90英寸）、6.70inch（不锈钢碗6.70英寸）'),
    ('10-20cm、30cm', '3.94-7.87inch、11.81inch'),
    ('固定14inch', '14.00inch'),
    ('10inch、12.5inch', '10.00inch、12.50inch'),
    ('3.7"，9.2"，10.75"，12.36"', '3.70inch，9.20inch，10.75inch，12.36inch'),
    ('4.9inch/8.7inch/12.4inch', '4.90inch/8.70inch/12.40inch'),
    ('10.5-15.8inch（4档可调）', '10.50-15.80inch（4档可调）'),
    ('8/10/12/14inch', '8.00/10.00/12.00/14.00inch'),
    ('7.5"-15.4"（5档可调）', '7.50inch-15.40inch（5档可调）'),
    ('3.75"（小号）、5.5"（中号）', '3.75inch（小号）、5.50inch（中号）'),
    ('9.09-13.78in可调', '9.09-13.78inch可调'),
    ('10.04inch（可调范围3.4–11.3inch）', '10.04inch（可调范围3.40–11.30inch）'),
    ('7.9inch-11.4inch-15inch', '7.90inch-11.40inch-15.00inch'),
])
def test_supported_composite(raw, expected):
    assert convert_composite_cm_inches(raw) == expected


@pytest.mark.parametrize('raw', [
    '缺x缺x缺 cm', '15cm、未知', '12cm、3kg',
    '17.5cm（玻璃碗9.9英寸）、17cm（不锈钢碗6.7英寸）',
    'M号:15、L号:20cm', '可调5-16英寸（约12.7-80cm）',
])
def test_ambiguous_is_unchanged(raw):
    assert convert_composite_cm_inches(raw) is None


def test_round_interval_keeps_unit_and_layout():
    assert round_interval_value('5.5-9.5in', 2) == '5.50-9.50in'
    assert round_interval_value('5.5-9.5 inch', 2) == '5.50-9.50 inch'
    assert round_interval_value('9.5-5.5in', 2) is None
    assert round_interval_value('5.5-9.5oz', 2) is None

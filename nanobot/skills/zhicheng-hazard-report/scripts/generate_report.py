#!/usr/bin/env python3
"""
智成安监 - 隐患排查报告生成器
根据用户上传的图片和描述，生成专业的HTML隐患排查报告
"""

import os
import json
import base64
from datetime import datetime
from pathlib import Path

# 报告模板路径
TEMPLATE_PATH = Path(__file__).parent.parent / "assets" / "report_template.html"
OUTPUT_DIR = Path(__file__).parent.parent / "output"


def load_template():
    """加载HTML模板"""
    with open(TEMPLATE_PATH, 'r', encoding='utf-8') as f:
        return f.read()


def save_image_from_base64(base64_data: str, filename: str, output_dir: Path) -> str:
    """保存Base64编码的图片"""
    img_dir = output_dir / "imgs"
    img_dir.mkdir(parents=True, exist_ok=True)

    # 如果是data URL格式
    if "," in base64_data:
        header, data = base64_data.split(",", 1)
    else:
        data = base64_data

    img_path = img_dir / filename
    with open(img_path, 'wb') as f:
        f.write(base64.b64decode(data))

    return str(img_path.relative_to(output_dir))


def save_uploaded_image(image_path: str, filename: str, output_dir: Path) -> str:
    """复制上传的图片到报告目录"""
    img_dir = output_dir / "imgs"
    img_dir.mkdir(parents=True, exist_ok=True)

    src_path = Path(image_path)
    dst_path = img_dir / filename

    if src_path.exists():
        import shutil
        shutil.copy(src_path, dst_path)

    return str(dst_path.relative_to(output_dir))


def create_point_card(point_num: int, name: str, scene: str, image_path: str,
                      hazards: list, has_severe: bool = False) -> str:
    """生成单个点位卡片HTML"""

    # 计算隐患数量
    severe_count = sum(1 for h in hazards if h.get('level') == '严重')
    general_count = sum(1 for h in hazards if h.get('level') == '一般')
    low_count = sum(1 for h in hazards if h.get('level') == '低')

    # 颜色标识
    if has_severe or severe_count > 0:
        card_class = "red"
        badge_class = "bg-red"
        badge_text = "含严重隐患"
    else:
        card_class = "blue"
        badge_class = "bg-blue"
        badge_text = "一般隐患" if general_count > 0 else "无隐患"

    # 生成隐患列表HTML
    hazard_items_html = ""
    for hazard in hazards:
        level = hazard.get('level', '一般')
        if level == '严重':
            dot_class = "dot-red"
            level_class = "level-severe"
        elif level == '一般':
            dot_class = "dot-orange"
            level_class = "level-general"
        else:
            dot_class = "dot-blue"
            level_class = "level-low"

        hazard_items_html += f'''
    <li class="hazard-item">
      <div class="hazard-dot {dot_class}"></div>
      <div class="hazard-text">
        <div class="desc">{hazard.get('description', '')}</div>
        <div class="refs">依据{hazard.get('reference', '')}</div>
      </div>
      <span class="hazard-level {level_class}">{level}</span>
    </li>'''

    # 生成标签
    tags_html = ""
    if severe_count > 0:
        tags_html += f'<span class="tag severe">⚠ {severe_count} 项严重隐患</span>'
    if general_count > 0:
        tags_html += f'<span class="tag general">🔶 {general_count} 项一般隐患</span>'

    # 生成停工建议（仅严重隐患）
    stop_note_html = ""
    if severe_count > 0:
        stop_note_html = f'''
  <div class="stop-note">
    <div class="stop-icon">🛑</div>
    <div><strong>【紧急停工建议】</strong><br>{has_severe}</div>
  </div>'''

    # 生成汇总栏
    summary_items = []
    if severe_count > 0:
        summary_items.append(f'<span class="summary-chip chip-severe">⚠ {severe_count} 项严重隐患</span>')
    if general_count > 0:
        summary_items.append(f'<span class="summary-chip chip-general">🔶 {general_count} 项一般隐患</span>')

    summary_html = ""
    if summary_items:
        summary_bar = '<div class="summary-bar">' + ''.join(summary_items)
        if severe_count > 0:
            summary_bar += '<span style="font-size:.68rem;color:var(--red)">立即停工整改</span>'
        else:
            summary_bar += '<span style="font-size:.68rem;color:var(--text2)">整改时限：3日内</span>'
        summary_html = summary_bar + '</div>'

    card_html = f'''
<!-- ============ 点位{point_num}：{name} ============ -->
<div class="card">
  <div class="card-header">
    <div class="case-num {card_class}">{point_num}</div>
    <div class="card-title">
      <h2>{name}</h2>
      <p>{scene}</p>
    </div>
    <span class="badge {badge_class}" style="flex-shrink:0;margin-top:2px">{badge_text}</span>
  </div>
  <div class="thumb-wrap">
    <img src="{image_path}" alt="{name}现场图片">
    <div class="thumb-overlay"></div>
    <div class="badge-tl">{tags_html}</div>
  </div>
  <ul class="hazard-list">
{hazard_items_html}
  </ul>
  {stop_note_html}
  {summary_html}
</div>'''

    return card_html


def generate_report(data: dict) -> str:
    """生成完整报告HTML"""

    template = load_template()

    # 基本信息
    report_date = data.get('date', datetime.now().strftime('%Y年%m月%d日'))
    location = data.get('location', '施工现场')
    total_points = data.get('total_points', 0)
    total_severe = data.get('total_severe', 0)
    total_general = data.get('total_general', 0)
    total_hazards = total_severe + total_general

    # 替换封面信息
    template = template.replace('2026年4月5日', report_date)
    template = template.replace('中国电建成都院智成安监系统 · 2026年4月5日',
                               f'中国电建成都院智成安监系统 · {report_date}')

    # 替换统计信息
    template = template.replace('⚠ 严重隐患 3 项', f'⚠ 严重隐患 {total_severe} 项')
    template = template.replace('🔶 一般隐患 16 项', f'🔶 一般隐患 {total_general} 项')
    template = template.replace('4 处点位', f'{total_points} 处点位')

    template = template.replace('<div class="num">4</div><div class="label">分析点位</div>',
                               f'<div class="num">{total_points}</div><div class="label">分析点位</div>')
    template = template.replace('<div class="num" style="color:var(--red)">3</div><div class="label">严重隐患</div>',
                               f'<div class="num" style="color:var(--red)">{total_severe}</div><div class="label">严重隐患</div>')
    template = template.replace('<div class="num" style="color:var(--orange)">16</div><div class="label">一般隐患</div>',
                               f'<div class="num" style="color:var(--orange)">{total_general}</div><div class="label">一般隐患</div>')
    template = template.replace('<div class="num" style="color:var(--blue)">19</div><div class="label">隐患总数</div>',
                               f'<div class="num" style="color:var(--blue)">{total_hazards}</div><div class="label">隐患总数</div>')

    # 替换总览
    template = template.replace('⚠ 严重隐患 3 项（立即停工整改）',
                               f'⚠ 严重隐患 {total_severe} 项（立即停工整改）')
    template = template.replace('🔶 一般隐患 16 项（限期3日内整改）',
                               f'🔶 一般隐患 {total_general} 项（限期3日内整改）')

    # 生成点位卡片
    points_html = ""
    for i, point in enumerate(data.get('points', []), 1):
        has_severe = any(h.get('level') == '严重' for h in point.get('hazards', []))
        points_html += create_point_card(
            point_num=i,
            name=point.get('name', f'点位{i}'),
            scene=point.get('scene', ''),
            image_path=point.get('image', ''),
            hazards=point.get('hazards', []),
            has_severe=point.get('stop_note', '')
        )

    # 插入点位卡片
    marker = '<!-- ============ 点位1：周界报警箱 ============ -->'
    template = template.replace(marker, points_html + '\n\n' + marker)

    # 生成统计表
    table_rows = ""
    total_severe_count = 0
    total_general_count = 0
    for i, point in enumerate(data.get('points', []), 1):
        severe = sum(1 for h in point.get('hazards', []) if h.get('level') == '严重')
        general = sum(1 for h in point.get('hazards', []) if h.get('level') == '一般')
        total = severe + general
        total_severe_count += severe
        total_general_count += general

        if severe > 0:
            name_style = 'color:var(--orange)'
            stop_action = '<span class="hazard-level level-severe">立即停工</span>'
        else:
            name_style = ''
            stop_action = '<span class="hazard-level level-general">3日内整改</span>'

        table_rows += f'''
      <tr>
        <td class="bb" style="{name_style}">{i}. {point.get('name', f'点位{i}')} {'⚠' if severe > 0 else ''}</td>
        <td class="td-center bb" style="color:var(--text2)">{severe if severe > 0 else '0'}</td>
        <td class="td-center bb" style="color:var(--orange)">{general}</td>
        <td class="td-center bb">{total}</td>
        <td class="bb">{stop_action}</td>
      </tr>'''

    # 替换统计表
    old_tbody = '''<tr>
        <td class="bb">1. 周界报警箱（弱电安防）</td>
        <td class="td-center bb" style="color:var(--text2)">0</td>
        <td class="td-center bb" style="color:var(--orange)">5</td>
        <td class="td-center bb">5</td>
        <td class="bb"><span class="hazard-level level-general">3日内整改</span></td>
      </tr>
      <tr>
        <td class="bb">2. 钢筋堆场（山区基础设施）</td>
        <td class="td-center bb" style="color:var(--text2)">0</td>
        <td class="td-center bb" style="color:var(--orange)">5</td>
        <td class="td-center bb">5</td>
        <td class="bb"><span class="hazard-level level-general">3日内整改</span></td>
      </tr>
      <tr>
        <td class="bb" style="color:var(--orange)">3. 高墩模板作业 ⚠</td>
        <td class="td-center bb" style="color:var(--red);font-weight:700">2</td>
        <td class="td-center bb" style="color:var(--orange)">4</td>
        <td class="td-center bb">6</td>
        <td class="bb"><span class="hazard-level level-severe">立即停工</span></td>
      </tr>
      <tr>
        <td class="bb">4. 龙湖·御湖境（城市住宅）</td>
        <td class="td-center bb" style="color:var(--text2)">0</td>
        <td class="td-center bb" style="color:var(--orange)">6</td>
        <td class="td-center bb">6</td>
        <td class="bb"><span class="hazard-level level-general">3日内整改</span></td>
      </tr>'''

    template = template.replace(old_tbody, table_rows)

    # 替换合计行
    template = template.replace(
        '<td class="td-center bt tfoot-td" style="color:var(--red)">3</td>\n        <td class="td-center bt tfoot-td" style="color:var(--orange)">16</td>\n        <td class="td-center bt tfoot-td" style="color:#fff">19</td>',
        f'<td class="td-center bt tfoot-td" style="color:var(--red)">{total_severe_count}</td>\n        <td class="td-center bt tfoot-td" style="color:var(--orange)">{total_general_count}</td>\n        <td class="td-center bt tfoot-td" style="color:#fff">{total_severe_count + total_general_count}</td>'
    )

    # 替换页脚日期
    template = template.replace('使用原始现场图片 · 2026年4月5日',
                               f'使用原始现场图片 · {report_date}')

    return template


def save_report(html_content: str, output_filename: str = None) -> str:
    """保存报告到文件"""

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if output_filename is None:
        output_filename = f"hazard_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"

    output_path = OUTPUT_DIR / output_filename

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html_content)

    return str(output_path)


def main():
    """测试报告生成"""

    # 示例数据
    sample_data = {
        'date': datetime.now().strftime('%Y年%m月%d日'),
        'location': '测试工地',
        'total_points': 1,
        'total_severe': 2,
        'total_general': 3,
        'points': [
            {
                'name': '高墩模板作业区',
                'scene': '桥梁工程 · 混凝土浇筑模板 · 高处临空作业',
                'image': 'imgs/test.jpg',
                'stop_note': '高墩临边作业无安全带+悬崖无护栏，存在高处坠落重大风险。建议立即停止该作业点施工。',
                'hazards': [
                    {
                        'level': '严重',
                        'description': '高处临边作业人员未佩戴安全带/安全绳，无任何防高处坠落措施',
                        'reference': '《建筑施工高处作业安全技术规范》（JGJ 80-2016）第3.0.5条'
                    },
                    {
                        'level': '严重',
                        'description': '悬崖边缘临边无安全防护栏杆，作业面完全暴露',
                        'reference': '《建筑施工高处作业安全技术规范》（JGJ 80-2016）第4.1.1条'
                    },
                    {
                        'level': '一般',
                        'description': '作业区域工具材料散落，高空落物风险高',
                        'reference': '《建筑施工高处作业安全技术规范》（JGJ 80-2016）第6.1.3条'
                    }
                ]
            }
        ]
    }

    html = generate_report(sample_data)
    output_path = save_report(html)

    print(f"报告已生成: {output_path}")
    return output_path


if __name__ == '__main__':
    main()

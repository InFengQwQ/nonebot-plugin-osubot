import json
import sys
import math
import argparse
from PIL import Image, ImageDraw, ImageFont

# ============================================================
#  可调配置项（修改以下数值即可控制排版，无需改动其余代码）
# ============================================================
H_SCALE = 0.8          # 横向缩放（轨道宽度、间隙、列间水平间距等）
V_SCALE = 1.5          # 纵向缩放（每拍像素高度、头部尺寸等）
TRACK_GAP = 1          # 轨道之间的水平间隙（基础值，会受 H_SCALE 影响）
# 以下为基础值，实际使用时会乘以 (命令行 --scale * 对应方向缩放)
BASE_TRACK_WIDTH = 80
BASE_PX_PER_BEAT = 140
BASE_NOTE_HEAD_HEIGHT = 12.5
BASE_CORNER_RADIUS = 4
BASE_TOP_MARGIN = 180
BASE_BOTTOM_MARGIN = 40
BASE_LEFT_MARGIN = 30
BASE_RIGHT_MARGIN = 30
BASE_COL_GAP = 160      # 列间水平间隙（用于绘制小节/BPM标注）
# ============================================================

# ---------- 辅助：节拍数组 -> 浮点总拍数 ----------
def beat_to_float(beat_arr):
    b, n, d = beat_arr[0], beat_arr[1], beat_arr[2]
    if d == 0:
        d = 1
    return b + n / d

def load_chart(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)

def parse_effect(ef):
    beat = ef.get('beat')
    for key in ef:
        if key == 'beat':
            continue
        if key == 'type':
            return ef['type'], ef.get('value', 1)
        val = ef[key]
        if isinstance(val, (int, float)):
            return key, val
    return None, None

def assign_to_columns(notes, time_points, effects, min_beat, max_beat, max_beats_per_col):
    N = math.ceil((max_beat - min_beat) / max_beats_per_col)
    if N == 0:
        N = 1
    col_starts = [min_beat + i * max_beats_per_col for i in range(N)]
    col_ends   = [min_beat + (i + 1) * max_beats_per_col for i in range(N)]
    col_ends[-1] = max_beat

    columns = []
    for i in range(N):
        columns.append({
            'start': col_starts[i],
            'end': col_ends[i],
            'notes': [],
            'time_points': [],
            'effects': [],
            'min_beat': float('inf'),   # 初始化为极大值
            'max_beat': -float('inf')
        })

    for note in notes:
        head_beat = beat_to_float(note['beat'])
        col_idx = 0
        for i, col in enumerate(columns):
            if head_beat >= col['start'] and (i == len(columns)-1 or head_beat < col['end']):
                col_idx = i
                break
        columns[col_idx]['notes'].append(note)
        end_beat = beat_to_float(note.get('endbeat', note['beat']))
        # 更新列的实际范围
        if head_beat < columns[col_idx]['min_beat']:
            columns[col_idx]['min_beat'] = head_beat
        if end_beat < columns[col_idx]['min_beat']:
            columns[col_idx]['min_beat'] = end_beat
        if end_beat > columns[col_idx]['max_beat']:
            columns[col_idx]['max_beat'] = end_beat
        if head_beat > columns[col_idx]['max_beat']:
            columns[col_idx]['max_beat'] = head_beat

    for tp in time_points:
        b = beat_to_float(tp['beat'])
        for i, col in enumerate(columns):
            if b >= col['start'] and (i == len(columns)-1 or b < col['end']):
                columns[i]['time_points'].append(tp)
                # 时间点也计入范围
                if b < columns[i]['min_beat']:
                    columns[i]['min_beat'] = b
                if b > columns[i]['max_beat']:
                    columns[i]['max_beat'] = b
                break

    for ef in effects:
        beat_val = ef.get('beat')
        if isinstance(beat_val, list):
            b = beat_to_float(beat_val)
        else:
            b = float(beat_val) if beat_val is not None else 0
        for i, col in enumerate(columns):
            if b >= col['start'] and (i == len(columns)-1 or b < col['end']):
                columns[i]['effects'].append(ef)
                if b < columns[i]['min_beat']:
                    columns[i]['min_beat'] = b
                if b > columns[i]['max_beat']:
                    columns[i]['max_beat'] = b
                break

    # 处理空列（没有内容的列保留默认边界）
    for col in columns:
        if col['min_beat'] == float('inf'):
            col['min_beat'] = col['start']
            col['max_beat'] = col['start']

    # 确保每列的实际范围至少包含列的理论边界，以便网格线覆盖全列
    for col in columns:
        if col['min_beat'] > col['start']:
            col['min_beat'] = col['start']
        if col['max_beat'] < col['end']:
            col['max_beat'] = col['end']
    return columns

def draw_rounded_rect(draw, xy, radius, fill):
    draw.rounded_rectangle(xy, radius=radius, fill=fill)

def render_preview_to_image(chart_data, show_events=False, show_groups=False,
                   max_beats_per_col=80, cmd_scale=1.0):
    meta = chart_data.get('meta', {})
    song = meta.get('song', {})
    mode_ext = meta.get('mode_ext', {})

    title = song.get('title', 'Unknown')
    artist = song.get('artist', '')
    creator = meta.get('creator', '')
    version = meta.get('version', '')
    bpm_main = song.get('bpm', 120)
    key = mode_ext.get('column', 4)

    notes = chart_data.get('note', [])
    time_points = chart_data.get('time', [])
    effects = chart_data.get('effects', [])

    if not notes and not time_points and not effects:
        # 返回一张空白图片以避免返回 None
        img = Image.new('RGB', (400, 100), '#1a1a1a')
        d = ImageDraw.Draw(img)
        d.text((20, 40), "Empty chart", fill='#ffffff')
        return img

    hold_count = sum(1 for n in notes if 'endbeat' in n)
    total_notes = len(notes)
    ln_ratio = (hold_count / total_notes * 100) if total_notes > 0 else 0.0

    all_beats = []
    for note in notes:
        all_beats.append(beat_to_float(note['beat']))
        if 'endbeat' in note:
            all_beats.append(beat_to_float(note['endbeat']))
    for tp in time_points:
        all_beats.append(beat_to_float(tp['beat']))
    for ef in effects:
        beat_val = ef.get('beat')
        if isinstance(beat_val, list):
            all_beats.append(beat_to_float(beat_val))
        elif isinstance(beat_val, (int, float)):
            all_beats.append(float(beat_val))
    if all_beats:
        min_beat = min(all_beats)
        max_beat = max(all_beats)
    else:
        min_beat, max_beat = 0, 1
    if max_beat <= min_beat:
        max_beat = min_beat + 1

    total_beats = max_beat - min_beat

    # ---------- 提前合并缩放参数（用于动态列数计算） ----------
    h_scale = H_SCALE * cmd_scale
    v_scale = V_SCALE * cmd_scale
    if bpm_main < 80:
        # 低 BPM 纵向拉伸，公式可调
        v_scale *= 2 * 80 / bpm_main

    px_per_beat = int(BASE_PX_PER_BEAT * v_scale)   # 每拍像素高度
    track_width = int(BASE_TRACK_WIDTH * h_scale)
    track_gap = int(TRACK_GAP * h_scale)
    col_gap = int(BASE_COL_GAP * h_scale)
    top_margin = int(BASE_TOP_MARGIN * v_scale)
    bottom_margin = int(BASE_BOTTOM_MARGIN * v_scale)
    left_margin = int(BASE_LEFT_MARGIN * h_scale)
    right_margin = int(BASE_RIGHT_MARGIN * h_scale)

    col_width = key * (track_width + track_gap) - track_gap
    base_height = total_beats * px_per_beat   # 不分列时的内容高度

    # ---------- 动态列数计算（宽高比约束） ----------
    RATIO_MIN = 1/2   # 0.5625
    RATIO_MAX = 2/3    # 0.6667

    best_N = None
    best_ratio_diff = float('inf')

    for N in range(1, 21):  # 最多尝试20列
        W = left_margin + N * (col_width + col_gap) - col_gap + right_margin
        H = top_margin + (base_height / N) + bottom_margin
        ratio = W / H
        if RATIO_MIN <= ratio <= RATIO_MAX:
            diff = abs(ratio - (RATIO_MIN + RATIO_MAX) / 2)
            if diff < best_ratio_diff:
                best_ratio_diff = diff
                best_N = N
        else:
            diff = min(abs(ratio - RATIO_MIN), abs(ratio - RATIO_MAX))
            if best_N is None or diff < best_ratio_diff:
                best_ratio_diff = diff
                best_N = N

    if best_N is None:
        best_N = 1

    dyn_max_beats_per_col = total_beats / best_N

    # ---------- 分配列 ----------
    columns = assign_to_columns(notes, time_points, effects, min_beat, max_beat, dyn_max_beats_per_col)

    # ---------- 其他尺寸参数 ----------
    note_head_height = int(BASE_NOTE_HEAD_HEIGHT * v_scale)
    corner_radius = max(1, int(BASE_CORNER_RADIUS * min(h_scale, v_scale)))
    note_head_width_ratio = 0.8
    hold_body_width_ratio = 0.64

    # ---------- 颜色定义 ----------
    COLOR_BG = '#1a1a1a'
    COLOR_GRID = '#3a3a3a'
    COLOR_BAR_LINE = '#5a5a5a'
    COLOR_NOTE_HEAD = '#4a90e2'
    COLOR_HOLD_BODY = '#7a7a7a'
    COLOR_TEXT = '#e0e0e0'
    COLOR_BPM_TEXT = '#5bc0de'
    COLOR_BAR_TEXT = '#aaaaaa'
    COLOR_LN_TEXT = '#f0a050'

    EFFECT_COLORS = {
        'scroll': '#ff6b6b',
        'speed':  '#ffd93d',
        'jump':   '#6bcb77',
        'sv':     '#ff6b6b',
        'hs':     '#ffd93d',
    }
    EFFECT_LABEL = {
        'scroll': 'SC',
        'speed':  'SP',
        'jump':   'JP',
        'sv':     'SV',
        'hs':     'HS',
    }
    DEFAULT_EFFECT_COLOR = '#cccccc'

    GROUP_COLORS = [
        '#4a90e2', '#e24a6e', '#4ae27a', '#e29b4a',
        '#9b4ae2', '#4ad3e2', '#e24ac0', '#b8a070',
        '#708090', '#a0b84a'
    ]

    # ---------- 计算画布尺寸 ----------
    total_width = left_margin + len(columns) * (col_width + col_gap) - col_gap + right_margin
    # 所有列采用统一的理论高度（基于每列的理论拍数范围）
    col_heights = []
    for col in columns:
        # 理论高度：使用 (end - start) 拍数，保证各列高度一致（最后一列可能略短，但影响小）
        height = (col['end'] - col['start']) * px_per_beat + int(60 * v_scale)
        col_heights.append(int(height))
    max_col_height = max(col_heights)
    total_height = top_margin + max_col_height + bottom_margin

    img = Image.new('RGB', (total_width, total_height), COLOR_BG)
    draw = ImageDraw.Draw(img)

    # ---------- 字体 ----------
    try:
        font_title = ImageFont.truetype("arial.ttf", 28)
        font_info = ImageFont.truetype("arial.ttf", 20)
        font_small = ImageFont.truetype("arial.ttf", 16)
        font_tiny = ImageFont.truetype("arial.ttf", 13)
    except:
        font_title = ImageFont.load_default()
        font_info = font_title
        font_small = font_title
        font_tiny = font_title

    # ---------- 顶部元信息 ----------
    info_x = left_margin
    info_y = int(15 * v_scale)
    draw.text((info_x, info_y), f"{title}  -  {artist}", fill=COLOR_TEXT, font=font_title)
    info_y += int(32 * v_scale)
    draw.text((info_x, info_y),
              f"Creator: {creator}   |   Version: {version}   |   Keys: {key}   |   BPM: {bpm_main}",
              fill=COLOR_TEXT, font=font_info)
    info_y += int(28 * v_scale)
    draw.text((info_x, info_y),
              f"Notes: {total_notes}   |   Time Points: {len(time_points)}   |   Effects: {len(effects)}",
              fill=COLOR_TEXT, font=font_small)
    info_y += int(22 * v_scale)
    draw.text((info_x, info_y), f"LN: {hold_count} ({ln_ratio:.1f}%)", fill=COLOR_LN_TEXT, font=font_small)

    if show_groups:
        info_y += int(28 * v_scale)
        groups = set()
        for n in notes:
            g = n.get('group')
            if g is not None:
                groups.add(g)
        groups = sorted(groups)
        legend_x = info_x
        legend_y = info_y + int(5 * v_scale)
        draw.text((legend_x, legend_y), "Groups:", fill=COLOR_TEXT, font=font_small)
        legend_x += int(80 * h_scale)
        for idx, g in enumerate(groups):
            color = GROUP_COLORS[idx % len(GROUP_COLORS)]
            rect_size = int(14 * v_scale)
            draw.rectangle([legend_x, legend_y, legend_x+int(18*h_scale), legend_y+rect_size],
                           fill=color, outline='#ffffff')
            draw.text((legend_x+int(22*h_scale), legend_y-int(1*v_scale)), str(g), fill=COLOR_TEXT, font=font_tiny)
            legend_x += int(50 * h_scale)
            if legend_x > total_width - int(200 * h_scale):
                legend_x = info_x + int(80 * h_scale)
                legend_y += int(18 * v_scale)

    # ---------- 绘制各列 ----------
    for col_idx, col in enumerate(columns):
        col_x = left_margin + col_idx * (col_width + col_gap)
        col_top = top_margin
        col_height = col_heights[col_idx]

        draw.rectangle([col_x, col_top, col_x+col_width, col_top + col_height],
                       outline='#404040', width=1)

        # ---------- beat 映射（底部对齐，内容范围填满列高） ----------
        content_min = col['min_beat']   # 最小 beat（将位于最下方）
        content_max = col['max_beat']   # 最大 beat（将位于最上方）
        content_beats = content_max - content_min
        content_px = content_beats * px_per_beat

        if content_px < col_height:
            # 底部对齐：内容整体下移，使得最小 beat 对齐列底部
            offset_y = col_height - content_px
            def beat_to_y(beat):
                return col_top + offset_y + (content_max - beat) * px_per_beat
        else:
            # 内容超出列高，直接映射（最小 beat 可能在列底部之外，但可视部分仍为底部对齐）
            def beat_to_y(beat):
                return col_top + (content_max - beat) * px_per_beat

        # ---------- 网格线（节拍线 + 1/4 分度线） ----------
        beat_start = math.ceil(content_min)
        beat_end = math.floor(content_max)
        for b in range(int(beat_start), int(beat_end)+1):
            y = beat_to_y(b)
            if col_top <= y < col_top + col_height:
                if b % 4 == 0:
                    draw.line([col_x, y, col_x+col_width, y], fill=COLOR_BAR_LINE, width=2)
                else:
                    draw.line([col_x, y, col_x+col_width, y], fill=COLOR_GRID, width=1)
        # 1/4 分度线
        # for frac in range(int(beat_start * 4), int(beat_end * 4) + 1):
        #     if frac % 4 != 0:
        #         y = beat_to_y(frac / 4.0)
        #         if col_top <= y < col_top + col_height:
        #             draw.line([col_x, y, col_x+col_width, y], fill='#2a2a2a', width=1)

        # 轨道分隔线
        for k in range(1, key):
            x = col_x + k * (track_width + track_gap) - track_gap//2
            draw.line([x, col_top, x, col_top+col_height], fill='#2a2a2a', width=1)

        # ---------- 音符 ----------
        for note in col['notes']:
            col_num = note.get('column', 0)
            head_beat = beat_to_float(note['beat'])
            head_y = beat_to_y(head_beat)

            note_left = col_x + col_num * (track_width + track_gap)
            note_center_x = note_left + track_width / 2

            has_end = 'endbeat' in note
            if show_groups:
                group_id = note.get('group', 0)
                head_color = GROUP_COLORS[group_id % len(GROUP_COLORS)]
            else:
                head_color = COLOR_NOTE_HEAD

            if has_end:
                end_beat = beat_to_float(note['endbeat'])
                tail_y = beat_to_y(end_beat)
                y0, y1 = sorted([head_y, tail_y])
                body_w = track_width * hold_body_width_ratio
                body_left = note_center_x - body_w / 2
                body_right = note_center_x + body_w / 2
                draw_rounded_rect(draw, [body_left, y0, body_right, y1],
                                  radius=corner_radius, fill=COLOR_HOLD_BODY)

            head_w = track_width * note_head_width_ratio
            head_left = note_center_x - head_w / 2
            head_right = note_center_x + head_w / 2
            head_rect = [head_left, head_y - note_head_height,
                         head_right, head_y]
            draw_rounded_rect(draw, head_rect, radius=corner_radius, fill=head_color)

        # ---------- 效果点 ----------
        if show_events:
            for ef in col['effects']:
                etype, val = parse_effect(ef)
                if etype is None:
                    continue
                beat_val = ef.get('beat')
                if isinstance(beat_val, list):
                    b = beat_to_float(beat_val)
                else:
                    b = float(beat_val) if beat_val is not None else 0
                y = beat_to_y(b)
                color = EFFECT_COLORS.get(etype, DEFAULT_EFFECT_COLOR)
                label = EFFECT_LABEL.get(etype, etype[:2].upper())
                r = int(6 * min(h_scale, v_scale))
                draw.ellipse([col_x - int(10*h_scale), y - int(6*v_scale),
                              col_x, y + int(4*v_scale)], fill=color, outline='#ffffff')
                draw.text((col_x + int(2*h_scale), y - int(6*v_scale)), label, fill=color, font=font_tiny)

        # ---------- 小节号（左侧） ----------
        bar_start = math.ceil(content_min / 4) * 4
        bar = bar_start
        while bar <= content_max:
            if bar >= content_min:
                y = beat_to_y(bar)
                bar_num = int(bar / 4) + 1
                draw.text((col_x - int(45*h_scale), y - int(7*v_scale)), str(bar_num),
                          fill=COLOR_BAR_TEXT, font=font_tiny)
            bar += 4

        # ---------- BPM 变化标注（右侧） ----------
        for tp in col['time_points']:
            b = beat_to_float(tp['beat'])
            y = beat_to_y(b)
            bpm_val = tp.get('bpm', '')
            draw.text((col_x + col_width + int(8*h_scale), y - int(7*v_scale)),
                      f"BPM {bpm_val}", fill=COLOR_BPM_TEXT, font=font_tiny)

    return img

# ---------- 供外部调用的接口 ----------
import io

def render_mc_to_bytes(input_path, show_events=False, show_groups=False,
                       max_beats_per_col=80, scale=1.0):
    """读取 .mc 文件，返回 PNG 图片的字节流"""
    chart = load_chart(input_path)
    img = render_preview_to_image(chart, show_events=show_events,
                                  show_groups=show_groups,
                                  max_beats_per_col=max_beats_per_col,
                                  cmd_scale=scale)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return buf.getvalue()

# 保留原 render_preview 函数作为命令行保存版本（可选）
def render_preview_save(chart_data, output_path, show_events=False, show_groups=False,
                        max_beats_per_col=80, cmd_scale=1.0):
    """保存预览图到文件（用于独立运行）"""
    img = render_preview_to_image(chart_data, show_events, show_groups, max_beats_per_col, cmd_scale)
    img.save(output_path)
    print(f"预览图已保存至: {output_path}")

def main():
    parser = argparse.ArgumentParser(description="导出 .mc 谱面的预览图")
    parser.add_argument("input", help="输入的 .mc 文件路径")
    parser.add_argument("-o", "--output", help="输出图片路径")
    parser.add_argument("--effect", action="store_true", help="显示效果点（scroll/speed/jump）")
    parser.add_argument("--group", action="store_true", help="按分组着色并显示图例")
    parser.add_argument("--max-beats-per-col", type=float, default=80, help="每列最大拍数")
    parser.add_argument("--scale", type=float, default=1.0, help="全局缩放倍率（会与内部 H/V_SCALE 相乘）")
    args = parser.parse_args()

    chart = load_chart(args.input)
    out_path = args.output or args.input.rsplit('.', 1)[0] + '_preview.png'
    render_preview_save(chart, out_path,
                   show_events=args.effect,
                   show_groups=args.group,
                   max_beats_per_col=args.max_beats_per_col,
                   cmd_scale=args.scale)

if __name__ == '__main__':
    main()
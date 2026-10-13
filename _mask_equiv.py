"""验证 MASK_DOWN 降采样后的遮罩几何是否与原实现等价。

对比两套实现产生的「窗口坐标下的不透明区域」：
  A. 原实现：全分辨率 bool 阵列 → np.roll(4)×3 → 每行 1px 高的 span
  B. 新实现：降采样 1/4 → np.roll(1)×3 → span 坐标×4、高×4
两者应当覆盖大致相同的像素集合。
"""
import numpy as np

W = H = 400
MASK_DOWN = 4

# 合成剪影：一个居中的圆盘 + 一条细发丝（考验「细结构会不会被降采样吃掉」）
yy, xx = np.mgrid[0:H, 0:W]
disc = ((yy - 210) ** 2 + (xx - 190) ** 2) < 150 ** 2
strand = (np.abs(xx - (250 + 40 * np.sin(yy / 40.0))) < 1) & (yy < 150)
silhouette = disc | strand


def region_spans(m, down):
    """返回 [(x, y, w, h), ...] 窗口坐标下的 span。"""
    m = m.copy()
    r = 1 if down else 4
    for _ in range(3):
        m = (m | np.roll(m, r, 0) | np.roll(m, -r, 0)
             | np.roll(m, r, 1) | np.roll(m, -r, 1))
    edges = np.diff(np.pad(m.astype(np.int8), ((0, 0), (1, 1))))
    out = []
    for y in range(m.shape[0]):
        starts = np.flatnonzero(edges[y] == 1)
        ends = np.flatnonzero(edges[y] == -1)
        for s, e in zip(starts, ends):
            if down:
                out.append((int(s) * MASK_DOWN, y * MASK_DOWN,
                            max(1, int(e - s) * MASK_DOWN), MASK_DOWN))
            else:
                out.append((int(s), y, max(1, int(e - s)), 1))
    return out


def covered_area(spans):
    """span 总覆盖面积（重叠不重复计，粗略足够做对比）。"""
    return sum(w * h for _, _, w, h in spans)


old = region_spans(silhouette, down=False)
new = region_spans(silhouette[::MASK_DOWN, ::MASK_DOWN], down=True)

a_old, a_new = covered_area(old), covered_area(new)
print(f"全分辨率 : {len(old):5d} spans, 覆盖面积 {a_old:8d} px²")
print(f"1/4 降采样: {len(new):5d} spans, 覆盖面积 {a_new:8d} px²")
print(f"面积比   : {a_new / a_old:.4f}  (1.0 附近即等价)")

# 把两套 span 光栅化到同一张图上，看差集
def raster(spans, down):
    g = np.zeros((H, W), dtype=bool)
    for x, y, w, h in spans:
        g[y:y + h, x:x + w] = True
    return g


g_old, g_new = raster(old, False), raster(new, True)
only_old = g_old & ~g_new      # 新遮罩漏掉的区域（会导致该处点不到桌宠本体）
only_new = g_new & ~g_old      # 新遮罩多盖的区域（会导致该处透不过来）
print()
print(f"只在原遮罩里（新遮罩漏掉）: {only_old.sum():7d} px  ({only_old.sum()/g_old.sum()*100:.1f}%)")
print(f"只在新遮罩里（新遮罩多盖）: {only_new.sum():7d} px  ({only_new.sum()/g_new.sum()*100:.1f}%)")
print()
print("原始剪影不透明像素:", int(silhouette.sum()))
print("降采样后取样到的不透明像素:", int(silhouette[::MASK_DOWN, ::MASK_DOWN].sum() * MASK_DOWN * MASK_DOWN),
      "(等效覆盖)")
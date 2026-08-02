# 字体资产

## 解析方式

渲染器**不**读这个目录里的固定文件名，也不硬编码系统字体路径。字体由 `config/font_policy.json` 按类别声明，由 `book_video_factory.font_resolver` 解析：

```text
operator override → style config → 平台字体目录 → 本目录（内置回退）
```

类别：`chinese_body` `chinese_title` `chinese_serif` `chinese_rounded` `chinese_handwriting` `english` `english_serif`。

查看本机实际解析结果：

```bash
python3 book_video_factory/scripts/doctor.py --profile local-render
```

## 两条容易踩的坑

**按 family 名选面，不按 index。** `.ttc` 是字体集合。`NotoSansCJK-Regular.ttc` 的 index 0 是 *Noto Sans CJK JP*，用它渲染简体中文会得到日文字形。简体中文对应的是 *Noto Sans CJK SC*。因此 policy 里每个 CJK 候选都必须声明所需 family，由解析器枚举集合去匹配，而不是相信某个 index 数字。

**候选顺序优先于来源。** 某类别的第一候选就是设计意图。`chinese_rounded` 首选内置的站酷快乐体，不应该因为系统装了通用无衬线就被顶掉；而 `chinese_body` 首选系统 Noto Sans CJK SC 是合理的。

同名字体在多处命中时，按 policy 搜索路径顺序、再按目录深度与字典序取第一个。文件系统遍历顺序不允许决定渲染结果，否则同一项目在两台都装了该字体的机器上会渲染出不同结果。

## 内置字体与许可

`BUNDLED_FONTS.json` 记录本目录每个字体二进制的 family、许可、上游来源与 sha256。

仓库分发字体二进制就承担了随附许可文本的义务（SIL OFL 1.1 明确要求）。因此：

- 只记录 `license_id` 而没有归档许可文本，算**版权缺口**。
- `doctor.py` 在一般 profile 下报 warn，在 `--profile public-release` 下直接 blocked。
- 出现在本目录但未登记在 `BUNDLED_FONTS.json` 的字体二进制同样会被报出来。

当前状态：**四个字体的二进制与 OFL 1.1 许可文本都已齐备，版权缺口为零。**

| 字体 | 家族名 | 许可文本 | 上游 |
| --- | --- | --- | --- |
| `SmileySans-Oblique.otf` | `Smiley Sans` | `SmileySans-OFL.txt` | atelier-anchor/smiley-sans v2.0.1 |
| `MaShanZheng-Regular.ttf` | `Ma Shan Zheng` | `MaShanZheng-OFL.txt` | googlefonts/mashanzheng |
| `ZCOOLKuaiLe-Regular.ttf` | `ZCOOL KuaiLe` | `ZCOOLKuaiLe-OFL.txt` | googlefonts/zcool-kuaile |
| `ZhiMangXing-Regular.ttf` | `Zhi Mang Xing` | `ZhiMangXing-OFL.txt` | googlefonts/zhimangxing |

每个条目的 sha256 都记在 `BUNDLED_FONTS.json` 里，`doctor.py` 会校验文件是否与记录一致。

### 家族名要写对

policy 候选里的 `family` 必须是字体的**家族名**，不是完整样式名。得意黑的家族名是 `Smiley Sans`，`Oblique` 是子族；写成 `Smiley Sans Oblique` 匹配不上，字体会被静默跳过而落到下一个候选。用这个命令核对任何字体的真实家族名：

```bash
python3 -c "from PIL import ImageFont; print(ImageFont.truetype('<路径>', 32).getname())"
```

## 新增内置字体的规矩

1. 确认许可允许再分发。
2. 把字体和它的许可文本一起放进本目录。
3. 在 `BUNDLED_FONTS.json` 补一条记录，包含真实的上游 URL 与 sha256。
4. 在仓库根 `.gitignore` 的字体允许清单里显式加一行——字体默认被忽略，避免生成或用户提供的字体误入仓库。
5. 在 `config/font_policy.json` 相应类别里加入候选，并声明 family 名。

---
title: "Book Video Factory：把一本书变成一条可审计的解说短视频"
date: 2026-07-30
type: photo
category: AI
cover: images/chaoyue-baisui-paper-collage.jpg
tags: [AI视频, Codex Skill, 图书解说, 工作流]
---

一个可安装的 Codex Skill：从一个干净的本地工作区开始，把中文图书解说短视频的整条生产链——选题证据、文稿、配音、画面、混音、QC、成本记账——全部纳入文件化、可审计的流程。

**两种风格模板，互不混用：**

- **双语编辑模板（原风格）**：3:4 画幅，真实授权书封 + 12 张审批静帧 + 本地配音 + 双语字幕，FFmpeg 确定性渲染，可选 9:16 衍生版。
- **VOX 风格纸拼贴解说（新增）**：9:16、人声驱动的 editorial 纸拼贴风，4–8 秒一个视觉节拍，支持 Gemini API 编程车道和 Google Flow 手工车道。

**设计原则：**

- 仓库不捆绑任何书封、音乐、人声或隐藏素材，所有内容输入都是项目本地、用户所有的。
- 脚本每一句可以绑定到 Claim 证据和渲染场景契约，审批与文件哈希绑定，改一个字审批即失效。
- 人类门禁不可跳过：选题、文稿、版权、母语审校、发布，每一步都要人签字。
- 成本账本只记录真实用量，缺失就标 `—`，绝不编造 token 数。

下面几张是仓库 `examples/` 里维护的成品展示封面：

![《被讨厌的勇气》纸拼贴风格封面](images/chaoyue-baisui-paper-collage.jpg)

![《边界》双语编辑模板封面](images/boundaries.jpg)

![《高敏感是种天赋》封面](images/highly-sensitive.jpg)

![《不讨好的勇气》封面](images/no-people-pleasing.jpg)

![《原生家庭》封面](images/original-family.jpg)

安装一行搞定：

```bash
npx skills add jaxxchen003/book-video-factory --skill book-video-factory -g -y
```

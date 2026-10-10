# Codex 会话导航（悬浮窗）

一个贴在屏幕右侧的**窄长条悬浮窗**，用来给 Codex 桌面版的超长对话做"目录"：
按轮次列出你说过的每一句话，点一下，**Codex 窗口会自动滚到那一轮并高亮**。

> **非官方工具**：本项目与 OpenAI 没有任何关联，也不是官方产品。
>
> **隐私**：程序只在你本机读取 Codex 的会话记录（`~/.codex/`）用于显示，
> **不联网、不上传、不修改**任何数据 —— 所有内容都留在你自己电脑上。
>
> **关于按键注入**：点击某一轮时，程序会向 Codex 窗口发送 `Ctrl+F` 加一段文本，
> 用 Codex 自带的查找来定位。这是它唯一"主动操作"的地方，其余全部只读。

<!-- 截图：发布前请自己截一张（注意抹掉对话内容），或删掉本行 -->

## 它能做什么

- **轮次目录** —— 每条对话拆成一轮一轮，显示 `编号 + 时间 + 提问前 15 字 + 回答前 20 字`
- **点击跳转** —— 点某一轮，Codex 会自己滚过去并高亮那段文字
- **Shift 分组标记** —— 按住 Shift 点击/拖动，给多轮刷上同一种浅色（浅红→黄→蓝→绿→紫循环），再点一次取消
- **快捷翻页** —— `▲` / `▼` 每次翻 10 轮；**长按** 直接跳到最上 / 最下一轮
- **悬停放大** —— 鼠标滑到哪一轮，那一轮的标题和提问会微微放大
- **自动跟随** —— 自动识别你最近在用的对话并跟随切换
- **搜索** —— 顶部搜索框可按关键词过滤轮次
- **全程悬浮** —— 无边框、可置顶、可收起成一条、可拖拽改大小，不占用浏览器标签

## 使用方法

直接下载 `CodexFloat.exe` 双击运行，**不需要安装 Python**。

| 操作 | 说明 |
| --- | --- |
| 拖动标题栏 | 移动窗口 |
| `📌` | 置顶开关（默认开） |
| `—` | 收起成一条 |
| `✕` / `Esc` | 关闭 |
| 拖右下角 `◢` | 调整大小 |
| 点某一轮 | 跳到 Codex 对应位置 |
| Shift + 点击 / 拖动 | 分组标记 |
| `▲` `▼` | 翻 10 轮；长按跳顶/底 |

## 系统要求

- Windows 10 / 11（x64）
- 已安装 **Codex 桌面版**（ChatGPT 桌面应用）并至少聊过一次
- 无需 Python、无需管理员权限

## 工作原理

- 读取 `~/.codex/sessions/**/rollout-*.jsonl`（Codex 的会话记录）解析出每一轮
- 读取 `~/.codex/state_5.sqlite` 的 `threads.recency_at` 判断你最近在用的对话
- 跳转时通过 `SendInput` 向 Codex 窗口发送 `Ctrl+F` + 一个"全对话唯一片段"，用 Codex 自带的查找定位

全程**只读**，不会修改 Codex 的任何数据。

## 已知限制

- 跳转依赖 Codex 自带的 `Ctrl+F` 查找；若某条对话里找不到唯一片段，会提示无法定位
- "自动跟随"依据的是你**最近互动过**的对话；只切换查看、不打字时 Codex 不写状态，可能跟不上（可在下拉里手动选）
- 仅支持 Windows

## 自己构建

```bat
git clone <this-repo>
cd codex-float
build.bat
```

或手动：

```bat
pip install pyinstaller
pyinstaller --noconfirm --onefile --noconsole --name CodexFloat ^
  --icon src/icon.ico --paths src --hidden-import nav_core src/float_nav.py
```

产物在 `dist\CodexFloat.exe`。

## 目录结构

```
src/float_nav.py    界面与交互（Tkinter 悬浮窗）
src/nav_core.py     会话解析（JSONL → 轮次，带文件级缓存与噪音过滤）
src/win_input.py    Windows 底层（窗口查找 + 键盘注入）
src/icon.ico        图标（含多尺寸）
build.bat           一键构建
```

## License

MIT




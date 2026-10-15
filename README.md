# Herdr Subscription Usage

Herdr 0.9+ 的常驻订阅用量窗格，Python 3.10+，无第三方依赖，无需修改 Herdr 源码。

| 订阅 | 凭据 | 显示 |
| --- | --- | --- |
| Zhipu CN | Coding Plan API Key | 5 小时、7 天剩余百分比及各自重置时间 |
| Kimi CN | Coding Plan API Key | 5 小时、7 天剩余百分比及各自重置时间 |
| Codex | 当前本机 Codex CLI 登录 | 5 小时、7 天剩余百分比及各自重置时间 |
| Cursor | 当前本机 Cursor 客户端或 Agent CLI 登录 | `30d` 月度剩余、`api` 剩余百分比、下次重置时间 |

## 显示位置

本插件使用普通插件终端窗格：已有较窄的左侧窗格时停靠在它上方；否则创建一个左侧停靠列。用量和操作按钮从顶部开始排列，工作终端保留在右侧，打开过程不抢走焦点。它不占用代理列表，也不要求存在运行中的代理。

窗格跟随 Herdr 的终端布局调整宽度，高度和宽度变化会触发重绘。较窄时缩短重置时间格式，较矮时优先去掉空行和附加金额，窗口过矮会明确提示。首次创建左列最多约 42 字符宽；随后随终端尺寸按比例变化，可用 Herdr 原有分隔线调整。

新窗口、新工作区及新标签页默认不打开用量窗格，只有按 `prefix+u` 或执行 `show` 才会在当前标签页打开，每个标签页最多一个；插件尊重用户的终端缩放模式，不主动解除 zoom。窗格按 `r` 手动刷新，当前标签页显示用量时默认每 60 秒自动查询。

标题栏提供可点击的 **设置**、**收起** 和 **关闭** 按钮。收起后保留左侧窄栏，点击 **展开** 恢复收起前的宽度；Herdr 原生分隔线的最小比例为 10%，因此窄栏宽度受此限制。窄栏也有 **×** 关闭按钮。

**关闭** 隐藏当前标签页的用量窗格并暂停订阅查询，自动更新和切换标签页不会让它重新弹出。当前标签页没有显示用量时不查询提供方；切换到仍显示用量的标签页或重新显示窗格后恢复自动查询。“收起”保留用量栏，仍继续刷新。按 **prefix+u** 再次显示并展开。也可以按 `c` 收起 / 展开。

标题和按钮使用青色，Zhipu / Kimi / Codex / Cursor 分别使用蓝 / 青 / 绿 / 紫色。剩余额度正常时为绿色，低于 30% 为黄色，低于 10% 为红色；缓存和待配置为黄色。重置时间使用加粗的终端默认前景色，辅助提示使用正常正文亮度，避免暗灰文字难以辨认。采用终端调色板，不强制背景色。

## 账号设置与多账号

点击 **设置**，选择一个账号即可编辑名称、Key 或登录路径。点击 **新增账号 +**，选择 Zhipu、Kimi、Codex 或 Cursor，可为同一家服务添加多个订阅账号；已有四家默认配置仍然有效，不需要迁移。

| 账号类型 | 每个附加账号需要的配置 |
| --- | --- |
| Zhipu / Kimi | 该账号独立的 Coding Plan API Key |
| Codex | 该账号的 `CODEX_HOME` 目录，例如 `~/.codex2` 或 `$HOME/.codex2` |
| Cursor | 该账号的 Agent CLI `auth.json` 路径，或客户端登录数据库路径 |

例如，已经用 `CODEX_HOME="$HOME/.codex2" codex login` 登录第二个账号后，在插件中选择 **设置 → 新增账号 + → Codex → 编辑 CODEX_HOME**，填入 `~/.codex2`，按 Enter 确定，再点击账号页的 **保存**。面板会同时显示 `Codex` 和 `Codex 2`，各自拥有独立的用量、重置时间和失败缓存。

Key 输入始终遮罩，名称和路径正常显示。支持粘贴、方向键、Backspace、Delete，以及 `Ctrl+U` 清空；已有值打开时处于选中替换状态。字段编辑完成按 Enter 确定，随后点击账号页的 **保存** 才会写入本机配置并刷新。取消或返回不会保存草稿。默认账号可停用，附加账号也可删除，删除前有一次确认。Key 和登录路径不会写入用量缓存。

键盘按 `s` 打开设置，`+` 新增账号，数字键选择当前页的账号。设置列表支持翻页；用量超出面板高度时，可用鼠标滚轮、↑ / ↓ 或 PageUp / PageDown 查看其它账号。

也可手动编辑 `config.json`，在已有四家配置之外增加 `accounts`：

```json
{
  "accounts": [
    {
      "id": "codex2",
      "provider": "codex",
      "name": "Codex 2",
      "codex_home": "$HOME/.codex2",
      "enabled": true
    }
  ]
}
```

这只是需要添加到现有配置中的字段。每个附加账号 `id` 必须唯一，`provider` 为 `zhipu`、`kimi`、`codex` 或 `cursor`；`name` 是可选显示名称。默认 `codex.codex_home` 留空时使用当前进程的登录配置，显式填写后则查询指定目录。`CODEX_HOME` 仅传给对应的 Codex 子进程，并行查询不会更改其它账号或终端的环境。完整示例见 [examples/multiple-accounts.json](examples/multiple-accounts.json)。

## 安装

其它设备需要先安装 **Herdr 0.9+、Python 3.10+ 和 Git**，并确保终端能运行 `herdr`、`python3` 和 `git`。打开 Herdr 后，在其中的终端执行：

```sh
herdr plugin install yinyajiang/herdr-subplan-usage
herdr plugin action invoke herdr-subplan-usage.init-config
herdr plugin action invoke herdr-subplan-usage.show
```

安装时按提示确认即可；无人值守安装可给第一条命令加 `--yes`。无需编译插件或修改 Herdr 源码。

首次使用点击窗格中的 **设置**，输入 Zhipu / Kimi 的 Coding Plan Key 并保存。Codex 必须在这台设备的 Codex CLI 中登录订阅账号，Cursor 可在这台设备的客户端中登录，也可运行 `agent login` 登录。登录和 Key 配置不会随 GitHub 安装自动同步；不使用的提供方可在配置中设为禁用。

如需 `prefix+u` 显示 / 展开用量，在这台设备的 Herdr `config.toml` 中添加以下条目（已有同一快捷键时先调整原条目）：

```toml
[[keys.command]]
key = "prefix+u"
type = "plugin_action"
command = "herdr-subplan-usage.show"
description = "subscription usage"
```

保存后执行 `herdr server reload-config`。快捷键配置不会随插件安装自动添加。

查看插件配置目录：

```sh
herdr plugin config-dir herdr-subplan-usage
```

### 本地开发安装

```sh
herdr plugin link /absolute/path/to/herdr-subplan-usage
herdr plugin action invoke herdr-subplan-usage.init-config
herdr plugin config-dir herdr-subplan-usage
```

可在界面中配置智谱和 Kimi Key，也可在输出目录的 `config.json` 中编辑 `api_key`，示例见 [config.example.json](config.example.json)。配置文件中的非空 Key 优先；没有文件 Key 时使用启动 Herdr 的 `ZHIPU_API_KEY` / `KIMI_API_KEY` 环境变量。不使用的提供方设 `enabled: false`。

```sh
herdr plugin action invoke herdr-subplan-usage.show
```

`show` 启动自动更新并建立当前标签页的停靠窗格，重复运行不会增加重复窗格。插件链接 / 启用不触发 Herdr startup hook，因此首次需要执行该动作，以后服务启动时只启动更新进程，不自动打开面板。可把 [examples/herdr-keybinding.toml](examples/herdr-keybinding.toml) 合并到 Herdr 配置，使用 `prefix+u` 补齐当前标签页的用量窗格。

从 0.1 升级时，移除 Herdr `[ui.sidebar.agents]` 中的 `$sp_*` 附加行；该版本不再向代理列表发布用量。停止旧的更新进程、重新链接，再执行 `show`。现有密钥配置不需要改动。

### 从 GitHub 升级

Herdr 0.9 的托管插件可通过重新安装获取最新代码，配置和状态目录会保留：

```sh
herdr plugin action invoke herdr-subplan-usage.stop
herdr plugin install yinyajiang/herdr-subplan-usage
herdr plugin action invoke herdr-subplan-usage.show
```

先等旧用量窗格关闭，再执行安装。若该设备之前使用 `plugin link` 安装，需要在停止后先执行 `herdr plugin unlink herdr-subplan-usage`，才能改用 GitHub 安装；本地源码和配置不会被删除。

## 刷新和停止

```sh
herdr plugin action invoke herdr-subplan-usage.refresh
herdr plugin action invoke herdr-subplan-usage.stop
```

`stop` 停止更新，并关闭该插件保存的自有用量窗格。工作终端和其他插件窗格不会被关闭。重新执行 `show` 恢复显示。通过按钮或 Herdr 原生命令关闭用量窗格后，都不会自动重新创建；需要时执行 `show` 或按 `prefix+u` 再次打开。

显示时默认每 60 秒查询，可配置 30–3600 秒，最多 4 个账号并发查询。多个 Herdr 会话共享提供方缓存，查询通过文件锁避免重叠；每个 socket 各有一个更新进程。健康状态位于 `HERDR_PLUGIN_STATE_DIR/sessions/<socket-hash>/health.json`，暂停查询时为 `idle`。

`—` 表示没有提供额度；`重置未知` 表示没有可识别的重置时间。`缓存` / `~` 表示上次成功的数据，查询失败最多保留 15 分钟；经过重置时间的缓存百分比会失效。超过 3 分钟未更新也会标记为缓存。所有时间按运行插件机器的本地时区显示。

## 登录配置

Codex 使用本机 `codex app-server` 的 `account/rateLimits/read`，凭据读取和续期由 Codex 自身完成，沿用 `CODEX_HOME`。`codex.command` 可指定 CLI 的绝对路径。CLI 必须对应要查询的登录配置；API Key 登录没有 ChatGPT 订阅额度。窗口按实际时长识别，不把 Spark 等模型专属额度误当订阅总额度。

Cursor 优先只读客户端数据库中的 `cursorAuth/accessToken`：

- macOS：`~/Library/Application Support/Cursor/User/globalStorage/state.vscdb`
- Linux：`${XDG_CONFIG_HOME:-~/.config}/Cursor/User/globalStorage/state.vscdb`
- Windows：`%APPDATA%/Cursor/User/globalStorage/state.vscdb`

没有可用客户端登录时，自动读取 `agent login` 保存的 `accessToken`，支持仅安装 CLI 的 SSH / WSL 设备：

- macOS：`~/.cursor/auth.json`
- Linux / WSL：`${XDG_CONFIG_HOME:-~/.config}/cursor/auth.json`
- Windows：`%APPDATA%/Cursor/auth.json`

可用 `cursor.database_path` 指定客户端数据库，或用 `cursor.cli_auth_path` 指定 CLI 凭据文件。显式指定路径后仅使用该登录，不自动切换账号。支持运行中的 WAL 数据；客户端登录过期时请打开 Cursor 刷新，CLI 登录过期时运行 `agent login`。插件只读凭据，不保存或更新登录 Token。月度额度优先采用个人订阅池的 `totalPercentUsed`（Included 已用百分比）计算月剩余，并用 `apiPercentUsed` 单独计算 API 剩余，面板分别显示为 `30d` 和 `api`；两项均为 100% 减去对应的已用百分比，API 数据缺失时显示“—”。基础金额计数可能已经满额，但不能覆盖 Included 百分比。没有此百分比时才回退到金额比例，不包含额外按量付费，也不把团队共享池当个人额度。

若 Codex CLI 已登录但显示 `HTTP 403`，表示用量接口拒绝当前请求，不代表没有订阅。请检查该设备到 `chatgpt.com` 的网络及代理。插件优先使用 Herdr 进程继承的代理环境；Linux / WSL 后台进程没有代理变量时，会从当前用户的交互式 Shell 环境补读 `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` 及其小写形式，并沿用 `NO_PROXY`。不会覆盖已有代理，也不会导入其它登录或配置变量。更改代理后执行 `stop`、等待用量窗格关闭，再执行 `show` 重新读取。插件不会回显 CLI 返回的 HTTP 错误正文。

配置和状态使用 Herdr 注入的 `HERDR_PLUGIN_CONFIG_DIR` / `HERDR_PLUGIN_STATE_DIR`。Unix 配置和缓存文件为 `0600`，不含密钥的示例才可放入仓库。Windows 需要 `python3` 可执行命令；如果仅安装了 `python`，请调整 manifest。

## 验证与接口

```sh
SUBPLAN_CONFIG=/path/to/config.json python3 main.py json
python3 -m unittest discover -s tests -v
```

使用 [Herdr 公共插件与窗格接口](https://herdr.dev/docs/plugins/)和 [Codex App Server](https://developers.openai.com/codex/app-server)。智谱查询 `open.bigmodel.cn/api/monitor/usage/quota/limit`，兼容 token / credit 两种额度；[Kimi 中国站](https://www.kimi.com/code/docs/)查询 `api.kimi.com/coding/v1/usages`，兼容比例池和旧计数格式；Cursor 查询 `cursor.com/api/usage-summary` 的个人订阅池和账单周期结束时间。

这些第三方用量端点可能变化，未提供或格式变化时会显示相应状态。当前不承诺智谱团队版或 Cursor 企业共享预算。带凭据的 HTTP 请求不跟随重定向；缓存和错误信息不保存 API Key、登录 Token、原始 HTTP 响应或用户身份。

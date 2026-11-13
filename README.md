# 作息表

> 夜间睡眠静默 + 早上回放留言

## 这是什么

让 bot 有自己的作息表：夜间静默不回消息，早上醒来把昨晚的留言一起回复

## 安装

```bash
cd AstrBot/data/plugins
git clone https://github.com/xiaoli0412/astrbot_plugin_sleep_schedule.git
```

然后在 WebUI 插件管理里启用，或重启 AstrBot。

## 配置

全部配置都能在 **WebUI → 插件 → 作息表 → 配置** 里改，不用手改文件。

| 键 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `enabled` | bool | `True` | 开启后，落在睡眠窗口内的被动消息不再回复，而是先存进待回放队列，醒来后统一回复。 |
| `sleep_window` | string | `23:30-07:30` | 格式 HH:MM-HH:MM，支持跨午夜，也支持多个窗口用逗号分隔，例如 23:30-07:30,13:00-14:00。 |
| `wake_check_seconds` | int | `60` | 后台任务每隔多少秒检查一次是否已经醒来；小于 5 秒按 5 秒处理。 |
| `max_replay_messages` | int | `20` | 一晚上留言特别多时只取最早的 N 条，剩下的留着下次继续回放。 |
| `replay_on_wake` | bool | `True` | 关闭后仅支持 /作息 回放 手动触发；开机时若队列里有留言仍会自动补一次。 |
| `fallback_persona_id` | string | `` | 会话没有绑定人格时使用的人格 ID（人格管理页面里那个人格的 ID）。留空表示不带人格。 |
| `whitelist_umos` | string | `` | 填完整 UMO，多个用逗号分隔。白名单里的会话夜里照常回复。 |
| `log_decisions` | bool | `True` | 拦截、回放等关键动作会写 info 日志，排查问题很方便。 |
| `replay_provider_id` | string | `deepseek/deepseek-flash` | 留空则用会话默认模型。会话默认通道可能超时（实测 gemini 渠道连续 5 次 APITimeoutError 导致回放永远失败），建议指 |

## 依赖

无第三方依赖（纯标准库）

## 兼容性

- 平台：*
- AstrBot：`>=4.24,<5`

## 隐私说明

插件只在本机运行，不上传任何数据到第三方。配置与运行时数据存在 `AstrBot/data/` 下。

## 许可

MIT，见 [LICENSE](LICENSE)。

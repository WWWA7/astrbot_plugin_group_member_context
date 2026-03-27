# astrbot_plugin_group_member_context

让 AstrBot 在群聊里自动感知并使用群成员身份信息。

## 作用

这个插件会在群聊消息进入 LLM 之前，自动查询并注入以下信息：

- 当前发言者的 QQ
- 当前发言者的昵称
- 当前发言者的群昵称 / 群名片
- 当前发言者的专属头衔
- 当前发言者的群身份（群主 / 管理员 / 普通成员）
- 当前群的群主是谁
- 当前群有哪些管理员
- Bot 自己在当前群里的身份

这样模型在回答下面这类问题时，就不会再乱猜：

- 你是管理吗
- 谁是群主
- 谁是管理员
- 我的头衔是什么
- 我的群昵称是什么
- 这个 QQ 是谁
- 这个人是不是管理

## 实现方式

插件使用 OneBot 11 / aiocqhttp 接口：

- `get_group_member_info`
- `get_group_member_list`

并在 `@filter.on_llm_request()` 阶段把真实群成员信息注入到系统提示词中。

此外还提供了一个 LLM 工具：

- `query_group_member_identity`

用于按关键词查询群成员身份信息。

## 适用环境

- AstrBot
- QQ / OneBot 11 / aiocqhttp 适配器
- 机器人具有正常获取群成员信息的能力

## 安装方法

把本目录放进 AstrBot 插件目录中，目录名保持为：

`astrbot_plugin_group_member_context`

然后在 AstrBot 插件管理中启用它。

## 配置项

### `inject_group_admin_list`
- 类型：布尔
- 默认：`true`

是否把管理员列表也一并注入给模型。

### `smart_query_group_snapshot`
- 类型：布尔
- 默认：`true`

开启后，插件只会在消息内容明显涉及这些话题时，才查询整群成员列表：

- 群主
- 管理员
- 管理权限
- 身份

这可以显著减少 `get_group_member_list` 的调用次数。

### `sender_info_ttl_seconds`
- 类型：整数
- 默认：`120`

当前发言者信息缓存秒数。  
也就是同一个群内、同一个用户，在缓存有效期内不会重复调用 `get_group_member_info`。

### `group_snapshot_ttl_seconds`
- 类型：整数
- 默认：`300`

群身份快照缓存秒数。  
用于缓存：

- 群主
- 管理员列表
- Bot 在当前群里的身份

在缓存有效期内，不会重复调用 `get_group_member_list`。

### `no_cache`
- 类型：布尔
- 默认：`false`

是否要求 OneBot 接口本身也尽量不使用缓存。  
通常建议保持关闭，让插件自己的内存缓存生效即可。

### `extra_instruction`
- 类型：文本
- 默认：空

会附加到插件自动注入的提示词后面，可用于追加你的个性化规则。

## 效果说明

例如用户在群里发送：

> 你是管理吗

插件会给模型补充类似这样的上下文：

- 当前发言者是谁
- 当前发言者的 QQ、昵称、群昵称、头衔、身份
- 当前群主是谁
- 当前管理员有哪些
- Bot 自己在当前群里是不是管理员 / 群主

于是模型就可以根据真实接口数据回答，而不是依赖记忆瞎猜。

## 资源消耗说明

不是每次 LLM 请求都会完整查询一遍群主和管理员信息。

当前策略是：

1. **发言者信息单独查**
   - 使用 `get_group_member_info`
   - 但有内存缓存，默认缓存 `120` 秒

2. **群主 / 管理员 / Bot 身份按需查**
   - 使用 `get_group_member_list`
   - 默认只有在消息内容涉及“群主、管理员、权限、身份”时才查
   - 并且默认缓存 `300` 秒

也就是说，像普通闲聊：

- 今天天气
- 讲个笑话
- 帮我写代码

这类消息通常**不会去查整群成员列表**。

只有像下面这种消息才更可能触发：

- 谁是群主
- 谁是管理员
- 你是管理吗
- 你有没有管理权限

所以资源消耗已经比“每轮都扫全群成员”小很多。

## 注意事项

1. 这个插件只增强“模型知道群成员身份信息”的能力，不会修改 AstrBot 原本的会话存储结构。
2. 如果你的平台不是 `AiocqhttpMessageEvent`，插件会自动跳过，不会报错中断。
3. 如果接口调用失败，插件会记录 warning 日志，并回退为不注入。
4. 如果你希望最省资源，建议：
   - 保持 `smart_query_group_snapshot = true`
   - 保持 `no_cache = false`
   - 适当提高 `sender_info_ttl_seconds`
   - 适当提高 `group_snapshot_ttl_seconds`

## 你这次需求对应的解决点

你给出的日志里，原始事件里其实已经有：

- `sender.role = owner`
- `sender.nickname`
- `sender.card`
- `sender.title`
- `sender.user_id`

但在 AstrBot 的后续会话处理中，这些信息没有稳定传递到模型侧，所以模型才会回答错误。

这个插件的目标就是在 **LLM 请求前重新实时查询并注入这些信息**，避免依赖框架中间层已经丢失的 `role=user` 结果。

## 文件结构

- `main.py`：插件主逻辑
- `metadata.yaml`：插件元信息
- `_conf_schema.json`：配置定义
- `README.md`：说明文档

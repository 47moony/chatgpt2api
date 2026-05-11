# chatgpt2api 二次开发功能说明

本文档记录当前分支相对原生 chatgpt2api 的主要二次开发内容、数据流、边界策略和本次 review 后收敛掉的风险点。

## 设计目标

当前分支把原生项目从“只消费 access token 的本地号池”扩展为：

- 注册机自动注册账号，并保存账号、密码、OAuth 凭据、代理和日志。
- 每个账号绑定一个代理，后续刷新账号信息和生图都尽量沿用该代理。
- 注册结果可导出为 sub2api 账号格式，支持带代理导出。
- 本地号池仍用于 chatgpt2api 生图消费，但 refresh token 的归属需要明确，避免 chatgpt2api 和 sub2api 同时续期同一账号。
- 账号异常时优先刷新 access token，必要时通过注册页保存的账号密码重新登录恢复。

## 与原生功能的主要区别

### 1. 账号池增加来源和代理字段

本地账号池记录新增了这些字段：

- `proxy_key` / `proxy`：账号绑定的代理。
- `credential_owner`：凭据归属，目前主要有 `chatgpt2api` 和 `sub2api`。
- `sub2api_account_id`：从 sub2api 导入时保留的远端账号 ID。
- `register_job_id`：注册机任务 ID。
- `oauth`：保存 `refresh_token`、`id_token`、`chatgpt_account_id` 等 OAuth 元数据。

账号池页面只额外展示“当前代理”和必要操作入口，密码不在号池页面显示。

### 2. 按账号代理刷新和生图

`OpenAIBackendAPI` 初始化时会根据账号记录选择代理：

- 优先使用账号自身 `proxy`。
- 其次用 `proxy_key` 从代理池查找。
- 最后回退到全局代理配置。

这影响：

- 一键刷新账号信息和额度。
- 生图/对话请求。
- 注册后导入本地号池的账号消费。
- 从 sub2api 导入且带代理的账号消费。

### 3. 注册机增强

注册服务从原生“一次性注册并入池”扩展为完整注册管理：

- 注册配置独立保存在 `data/register.json`。
- 注册日志按任务写入 `data/register_logs/{job_id}.jsonl`。
- 支持 `total`、`quota`、`available` 三种运行模式。
- 支持多邮箱 provider 轮换，并记录 `mail_provider`、`mail_provider_ref`、`mailbox` 元数据。
- 注册线程会从代理池随机分配代理，并把该代理写入注册结果。
- 注册成功后始终保存账号密码和 OAuth 信息。
- “注册后加入本地号池”作为可选开关，开启时同时导入 chat 本地号池；关闭时只保留注册记录，供导出 sub2api。

注册页新增“已注册号码”区域：

- 显示邮箱、密码、注册方式、代理、本地池导入状态、账号状态、注册时间。
- 支持按邮箱搜索。
- 支持删除注册历史。
- 支持将注册记录导入本地号池。
- 支持对需要恢复的账号重新登录恢复凭据。

### 4. 注册和登录重试

注册流程增加了对临时错误的重试：

- `password_verify_http_409`
- HTTP `408/409/425/429/5xx`
- `timeout`
- `connection`
- `proxy`
- `tls` / `ssl`
- 临时 token 换取失败

典型可重试场景：

- OpenAI 登录状态同步延迟。
- 代理链路临时失败。
- TLS 握手错误。
- 刚创建账号后独立登录阶段状态不稳定。

典型不应盲目重试的场景：

- 邮箱域名被风控或封禁导致的注册 400。
- 账号要求补手机号。
- refresh token 已失效且没有可用邮箱验证码元数据。
- 账号密码缺失。

### 5. 账号凭据恢复

恢复入口有两个：

- 注册页“已注册号码”区域。
- 号池管理页异常账号的“重新登录恢复”。

恢复逻辑使用注册记录中的邮箱、密码、邮箱服务元数据和代理重新登录，拿到新的：

- `access_token`
- `refresh_token`
- `id_token`
- `chatgpt_account_id`

保护策略：

- 注册页只有检测到缺凭据、鉴权失败、导出检查阻断时才允许恢复。
- 号池管理页只有账号已标记为“异常”且能匹配注册记录时才允许恢复。
- 后端也会拒绝未标记异常的号池账号恢复请求，避免绕过 UI 误触发重登。
- 如果恢复的账号已经导入本地号池，会自动同步更新该账号在本地号池中的凭据。

### 6. 一键刷新账号信息和额度

原生逻辑主要是使用当前 access token 请求 ChatGPT backend 获取额度和账号信息。

当前分支增加了：

- 远端查询失败的重试。
- 按账号代理刷新。
- 认证失败时，对 `credential_owner=chatgpt2api` 且有 refresh token 的账号尝试刷新 access token。
- refresh token 也失败时标记异常，不直接删除来源明确的账号。
- 对 `credential_owner=sub2api` 或带 `sub2api_account_id` 的账号，不在 chatgpt2api 内使用 refresh token，避免和 sub2api 争夺 refresh token 续期权。

删除策略：

- 手动一键刷新失败不会直接删除账号。
- 来源明确的账号遇到 invalid token 只标记“异常”。
- 没有来源标记的旧裸 token 仍遵循 `auto_remove_invalid_accounts` 配置。

### 7. sub2api 导出

sub2api 导出入口保留在注册页，不再从“号池管理”导出。

原因：

- 号池管理是消费池，里面的 access token 可能已被 chatgpt2api 消费或过期。
- 注册页保存的是完整注册记录，包含账号密码、refresh token、id token 和邮箱元数据，更适合生成 sub2api 导入包。

导出能力：

- 导出当前任务或筛选/选中的注册账号。
- 支持不带代理 JSON。
- 支持带代理 JSON。
- 导出前检查必要字段。
- 后端导出接口也会过滤字段不完整的账号，防止绕过前端检查导出坏数据。

导出必要字段：

- `access_token`
- `refresh_token`
- `id_token`
- `chatgpt_account_id`
- `client_id`

### 8. sub2api 导入

设置页新增 sub2api 连接管理：

- 配置 sub2api 服务器地址、管理员账号密码或 API key。
- 浏览远端账号组。
- 浏览远端 OpenAI OAuth 账号。
- 批量导入到本地号池。

导入策略：

- 从 sub2api 导入的账号标记 `credential_owner=sub2api`。
- 写入 `sub2api_account_id`，用于后续更新匹配。
- 读取并保存远端代理信息，供本地生图时按账号代理请求。
- 不把 sub2api 的 refresh token 交给 chatgpt2api 管理。

这样 sub2api 继续负责 refresh token 续期，chatgpt2api 只消费当前 access token。

## 关键数据文件

- `data/accounts.json`：本地号池。
- `data/register.json`：注册配置、注册结果、账号密码和 OAuth 元数据。
- `data/register_logs/*.jsonl`：注册任务日志。
- `data/sub2api_config.json`：sub2api 连接配置。
- `data/sub2api-proxy.json`：代理池配置。

## 推荐工作流

### 一次性生图账号

1. 在注册页开启“注册后加入本地号池”。
2. 启动注册。
3. 注册成功后账号自动进入本地号池。
4. chatgpt2api 按账号代理消费 access token 生图。
5. 账号异常后先一键刷新，必要时在号池页面恢复。

### 稳定交给 sub2api 管理

1. 在注册页关闭“注册后加入本地号池”。
2. 启动注册。
3. 在注册页导出 sub2api JSON，按需选择带代理。
4. 导入 sub2api。
5. 后续由 sub2api 管理 refresh token 续期。

### 从 sub2api 回导 chatgpt2api 消费

1. 在设置页配置 sub2api 连接。
2. 浏览远端账号并选择导入。
3. 导入本地号池后，chatgpt2api 只消费 access token。
4. refresh token 仍由 sub2api 负责。

## 本次 review 后已收敛的风险点

- 移除了“号池管理”侧 sub2api 导出后端入口和前端 API，避免从消费池导出失效凭据。
- 限制 `add_account_records` 的按邮箱覆盖逻辑：只有 `credential_owner=chatgpt2api` 的注册来源账号才允许按邮箱替换旧 token；sub2api 来源只按 `sub2api_account_id` 或 access token 更新，避免不同系统同邮箱账号误合并。
- 删除未使用的 `_needs_chatgpt_account_id` helper。
- 注册页状态展示不再把导出检查结果写入主状态，导出检查只作为提示和恢复按钮依据。
- 注册页恢复成功后只合并目标邮箱记录，不再用整份注册配置覆盖所有行。
- 后端注册导出接口增加字段完整性过滤，直接访问导出 URL 也不会导出缺字段账号。
- 号池恢复入口前后端都限制为异常账号，避免正常账号被误重登。

## 仍需注意的边界

- 重新登录恢复会触发真实登录流程，可能触发邮箱验证码、风控或手机号要求，应只在账号确认异常后使用。
- 如果旧注册记录缺少邮箱服务元数据，自动恢复可能无法读取登录验证码，需要人工处理。
- `token_invalidated` 首先表示 access token 被服务端作废；如果 refresh token 也不可用，只能重新登录。
- sub2api 和 chatgpt2api 不应同时管理同一账号的 refresh token。
- 代理质量会显著影响注册和生图稳定性，TLS/连接错误通常优先检查代理。


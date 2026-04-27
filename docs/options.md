# 配置项说明

下面只解释真正会影响业务的字段。

## run.count

本次任务最多尝试多少轮注册。

- `50`：跑 50 轮后自动退出
- `1`：做一次验证
- 不建议在控制台里直接配无限循环

## browser_proxy

浏览器访问 `x.ai` 时使用的代理。

什么时候要填：

- 服务器直连 `x.ai` 不通
- 直连能通，但 IP 容易被风控
- 你已经有本地 WARP/代理桥接，希望浏览器固定从那个出口出去

最常见写法：

- `http://127.0.0.1:18118`
- `socks5://127.0.0.1:1080`
- `socks5://warp:1080`

## proxy

普通 HTTP 请求走的代理，主要给临时邮箱 API 用。

它和 `browser_proxy` 不一定相同，但在大多数场景下建议保持一致，避免：

- 浏览器从香港出口访问 `x.ai`
- 邮箱 API 却从本机直连

这种前后链路不一致会增加排障难度。

## temp_mail_api_base

邮箱服务的接口地址。

示例：

- `https://doc.skymail.ink`（Cloud Mail）
- `https://api.duckmail.sbs`（DuckMail）
- `https://mail-api.example.com`（自定义 Temp Mail）

执行器会根据 `temp_mail_provider` 选择对应的接口调用方式。不同 provider 的接口契约不同，详见 [temp-mail-api.md](temp-mail-api.md)。

## temp_mail_provider

选择邮箱后端类型。可选值：

- `mailbox_system`：Cloud Mail 系统，使用 `genToken` / `addUser` / `emailList` 接口
- `duckmail`：DuckMail，使用 `/accounts` / `/token` / `/messages` 接口
- `generic`：旧版 Temp Mail，使用 `/admin/new_address` / `/api/mails` 接口
- 留空：自动检测（根据 API base URL 的 hostname 判断）

## temp_mail_admin_email

管理员邮箱，用于 Cloud Mail (`mailbox_system`) 的 `genToken` 接口。

如果你用的是 `mailbox_system`，这个字段必填。

DuckMail 和 generic provider 不需要此字段。

## temp_mail_admin_password

邮箱后台管理口令。

不同 provider 的含义：

- `mailbox_system`：管理员密码，用于 `genToken` 接口获取 token
- DuckMail：可留空（公共域名）；私有域名场景填 API Key
- generic：必填，创建邮箱时放在 `x-admin-auth` 头里

## temp_mail_domain

注册时实际使用的邮箱域名后缀。

例如：

- `mail.example.com`

这个字段很关键。就算邮箱 API 可用，如果这个域名本身被 `x.ai` 拒绝，流程也会卡在注册页。

- `mailbox_system`：必填
- DuckMail：可留空，执行器会自动从 DuckMail 域名列表里挑一个公开、已验证域名
- generic：必填

## temp_mail_site_password

仅旧版 generic provider 使用。如果你的接口没有站点级鉴权要求，留空即可。`mailbox_system` 和 DuckMail 可忽略此字段。

## api.endpoint

注册成功后用于接收 token 的管理接口。

典型示例：

- `http://127.0.0.1:8000/v1/admin/tokens`
- `http://grok2api:8000/v1/admin/tokens`

如果留空，任务仍然能注册，但不会自动入池。

## api.token

调用 sink 管理接口时的鉴权口令。

## api.append

决定推送 token 时是“保护存量后追加”，还是“直接覆盖”。

- `true`：先读取线上已有 token，再把本次结果合并去重后回写。适合生产环境。
- `false`：不读存量，直接用本次结果覆盖远端。只建议在测试环境里使用。

## 系统默认配置 vs 任务覆盖

两者不冲突，规则很简单：

1. 系统默认配置是全局底板
2. 新建任务时如果不展开高级设置，就直接继承系统默认值
3. 任务里填写了某个覆盖字段，只有那个任务会改，不会回写系统默认配置

所以更推荐的使用方式是：

- 把稳定不变的东西填在系统默认配置
- 只把这一次临时要变的参数填在任务高级设置

# 微博超话签到 WebUI

一个运行在 Linux 服务器上的单账号微博超话自动签到工具。通过粘贴浏览器复制的 Cookie 请求头导入账号，不使用用户名密码登录，也不需要 Chromium、Selenium 或 Playwright。

## 功能

- 首次访问设置管理员密码，后续使用密码登录 WebUI
- 设置页支持验证当前密码后修改管理员密码，修改成功需要重新登录
- 直接导入 Cookie 请求头，服务器只保存 Fernet 加密后的 Cookie
- 使用 m.weibo.cn/api/config 验证登录状态
- 从 m.weibo.cn/api/container/getIndex 分页同步关注的超话
- 逐个执行微博返回的合法签到 scheme
- 新同步的超话默认关闭，已有启用状态会保留
- 手动签到、取消任务、任务日志和历史记录
- 按 Asia/Shanghai 时间每天自动签到
- 配置中心：签到间隔、单次上限、连续失败停止、请求超时和读取重试
- 403/429 触发保护后自动冷却至次日零点
- QQ 官方 Bot API 私聊通知，支持完成、失败和风控事件汇总
- QQ Bot Gateway 私聊事件监听，自动发现并显示 `user_openid`，不保存私聊正文
- SQLite 持久化账号状态、超话选择、调度设置和运行记录
- 所有微博 HTTP 调用都集中在可测试的适配器中

## 安全边界

Cookie 等同于登录凭证。不要把 WebUI 暴露到公网，也不要把 Cookie 粘贴到日志、工单或聊天中。默认配置是 HTTP，适合受信任的内网；公网或跨网络部署应在反向代理后启用 HTTPS，并设置 APP_COOKIE_SECURE=true。

APP_SECRET_KEY 用于加密数据库里的 Cookie。生产环境必须固定设置并妥善备份；更换它会导致现有 Cookie 无法解密。管理员密码只保存 Argon2id 哈希。

微博 Cookie 没有一个由本工具决定的固定有效期，实际由微博会话、设备和风控策略决定。验证失败时重新导入 Cookie 即可。

## 本地运行

需要 Python 3.11 或更高版本。

    cd weibo-checkin-web
    python3 -m venv .venv
    . .venv/bin/activate
    python -m pip install -e .

    export APP_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
    uvicorn app.main:app --host 0.0.0.0 --port 8000

浏览器打开 http://服务器地址:8000。第一次访问会要求创建管理员密码。

## 使用流程

1. 登录后，在“微博账号”区域粘贴完整 Cookie 请求头，例如 Cookie: SUB=...; SUBP=...。
2. 点击验证 Cookie，确认账号状态为“登录有效”。
3. 点击同步超话，在列表中勾选需要自动签到的超话。
4. 点击立即签到，或在每日计划中设置时间。
5. 在“设置”中调整运行策略；需要通知时填写 QQ Bot 的 AppID、ClientSecret 和目标 `user_openid`，保存后可发送测试通知。
6. 如果不知道 `user_openid`，先填写 AppID 和 ClientSecret，开启“监听 QQ 私聊事件”并保存；在 QQ 开放平台开通 C2C/PUBLIC_MESSAGES 事件权限后，给机器人发送任意一条私聊消息，页面会自动显示发现的 `user_openid`，点击“使用”后再保存通知配置。

Cookie 请求头可以从浏览器开发者工具的 Network 请求 Headers 中复制。只导入自己拥有或获授权使用的账号。

## 配置

复制 .env.example，按需设置环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| APP_SECRET_KEY | 自动生成 | Cookie 加密密钥，生产环境必须固定 |
| APP_DATA_DIR | ./data | SQLite 和自动生成密钥的位置 |
| APP_DB_PATH | data/weibo-checkin.sqlite3 | SQLite 文件路径 |
| APP_HOST | 0.0.0.0 | 监听地址 |
| APP_PORT | 8000 | 监听端口 |
| APP_COOKIE_SECURE | false | HTTPS 部署时设为 true |
| APP_TIMEZONE | Asia/Shanghai | 调度使用的时区 |
| APP_CHECKIN_DELAY_SECONDS | 10.0 | 两次签到请求之间的间隔；WebUI 保存的值优先 |
| APP_MAX_TOPICS_PER_RUN | 0 | 单次最多处理的超话数量，0 表示不限制 |
| APP_MAX_CONSECUTIVE_FAILURES | 3 | 连续失败后停止，0 表示不启用 |
| APP_REQUEST_TIMEOUT_SECONDS | 15 | 微博请求超时秒数 |
| APP_READ_RETRY_COUNT | 1 | 读取接口最多重试 0-2 次，签到请求不重试 |
| APP_COOLDOWN_ON_RATE_LIMIT | true | 403/429 后是否冷却至次日零点 |

WebUI 保存的运行配置位于 SQLite 中，并优先于环境变量。恢复默认会重新使用当前环境变量提供的默认值，同时清除 QQ 通知凭证。QQ ClientSecret 以 APP_SECRET_KEY 加密保存，接口不会回显密钥；页面中留空表示保留原值。

QQ Gateway 监听需要服务器能够访问 `api.bot.qq.com`，并且机器人在 QQ 开放平台开启 C2C/PUBLIC_MESSAGES 事件权限。监听器使用官方 Gateway WebSocket，断线时会尝试恢复会话；服务只保存事件中的 `author.user_openid` 及发现时间，不保存私聊内容。

## systemd

参考 deploy/weibo-checkin.service。常见安装方式：

    sudo useradd --system --home /opt/weibo-checkin-web --shell /usr/sbin/nologin weibo-checkin
    sudo mkdir -p /opt/weibo-checkin-web
    sudo chown -R weibo-checkin:weibo-checkin /opt/weibo-checkin-web

将项目复制到 /opt/weibo-checkin-web，并创建 .venv、安装依赖：

    sudo -u weibo-checkin python3 -m venv /opt/weibo-checkin-web/.venv
    sudo -u weibo-checkin /opt/weibo-checkin-web/.venv/bin/pip install -e /opt/weibo-checkin-web

    sudo install -m 0644 deploy/weibo-checkin.service /etc/systemd/system/weibo-checkin.service
    sudo systemctl daemon-reload
    sudo systemctl enable --now weibo-checkin
    sudo systemctl status weibo-checkin

部署时建议通过 /etc/weibo-checkin.env 设置 APP_SECRET_KEY，并限制该文件权限为 0600。

## 测试

测试不会连接真实微博，所有 HTTP 请求均使用 mock transport：

    python -m pytest

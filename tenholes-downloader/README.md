# 蓝调口琴网曲谱拉取工具

本目录包含两个脚本，用于保存当前账号在蓝调口琴网中已经有权访问的曲谱及配套音频：

- `download_new_first_3.sh`：按“最新”顺序处理前 3 页，适合先验证登录和输出结果；
- `download_all.sh`：遍历完整曲谱列表，适合在验证后执行完整备份。

请只拉取你已获授权且可以合法留存的内容。脚本不用于绕过网站的会员、付费或其他权限控制。

## 环境要求

- Bash
- curl
- Ruby（只使用标准库）
- 支持并发参数 `-P` 的 `xargs`

macOS 和常见 Linux 发行版通常已经具备这些工具。

## 使用方法

在本目录运行前三页版本：

```shell
./download_new_first_3.sh
```

脚本随后显示 `ten_auth1 cookie value:`。粘贴 Cookie 值并按回车；终端不会显示输入内容。Cookie 只在本次进程及其下载子进程中使用，不会被写入配置文件。

确认输出无误后运行完整版本：

```shell
./download_all.sh
```

可通过环境变量调整并发数，但不要把 Cookie 写进环境变量或命令行参数：

```shell
TENHOLES_CONCURRENCY=4 ./download_all.sh
```

## 获取 Cookie 值

1. 在浏览器中正常登录蓝调口琴网，并确认账号有权查看目标内容。
2. 在浏览器开发者工具的存储或 Cookie 面板中找到 `ten_auth1`。
3. 只复制该 Cookie 的值，在脚本的隐藏输入提示中粘贴。

不要把整段 Cookie 请求头、浏览器导出文件或截图保存到仓库。不要把 Cookie 发到 Issue、聊天记录或 CI。

## 输出结构

两个脚本分别写入：

```text
output/
├── new_first_3_pages/
└── all_tracks/
```

目录中包括按曲目整理的图片和音频、`index.tsv`、失败清单，以及用于断点继续的 `.work/` 缓存。`output/` 已被项目根目录的 `.gitignore` 排除。

如果网络中断，可直接重新运行相同脚本。已有的部分文件和缓存会在适用时复用；失败项记录在对应输出目录的 `failures.tsv`。

## 安全提醒

脚本当前使用蓝调口琴网提供的 HTTP 地址，Cookie 在传输链路上无法获得 HTTPS 的加密保护。请只在可信网络中运行，使用后退出网站登录或在账户侧使旧会话失效。运行时不要启用 `set -x`，否则凭据可能出现在终端日志中。

请同时阅读项目根目录的 [`SECURITY.md`](../SECURITY.md)。

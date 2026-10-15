# Afdian 曲谱下载器

这是一个独立的 Python 工具，用于下载以下爱发电专辑中，当前账号**有权查看**的曲谱图片：

| 专辑 | 专辑 ID | 默认输出目录 |
| --- | --- | --- |
| 十孔口琴 | `11beb000f14f11ec800352540025c377` | `output/十孔口琴/` |
| 半音阶口琴 | `5dc07fbaf1cc11ec896052540025c377` | `output/半音阶口琴/` |
| 哨笛 | `73f44a40b65411eca6d752540025c377` | `output/哨笛/` |

运行时只使用 Python 标准库，不需要安装第三方依赖。要求 Python 3.9 或更高版本。

请只下载自己已经获得访问权限、并可合法留存的内容。程序不会尝试绕过爱发电的权限判断。

## 快速开始

进入本目录：

```shell
cd afdian-downloader
```

建议先做一次试运行。程序会安全提示输入 `auth_token`，输入内容不会显示：

```shell
python3 download.py --dry-run
```

确认统计无误后开始下载：

```shell
python3 download.py
```

默认会依次处理所有默认专辑。也可以只处理一个：

```shell
python3 download.py --album ten-hole
python3 download.py --album chromatic
python3 download.py --album whistle
```

## Token 使用方式

程序只从以下两个来源读取 token：

1. 环境变量 `AFDIAN_AUTH_TOKEN`；
2. 若环境变量不存在，则用 `getpass` 隐藏提示读取。

没有 `--token` 命令行参数，token 不会写入配置、输出清单或日志。最简单且不留 shell 历史的方式，就是让程序提示输入。

如果需要环境变量，可先隐藏读取，再运行：

```shell
read -rs AFDIAN_AUTH_TOKEN
export AFDIAN_AUTH_TOKEN
python3 download.py
unset AFDIAN_AUTH_TOKEN
```

认证 Cookie 只用于配置的 Afdian API 源站；程序会拒绝携带它跳转到其他源站，也不会把它发送给图片 CDN。
正式读取专辑前还会调用会话校验接口；无效或过期的 token 会直接报错，不会被误当成“全部曲目锁定”。

## 只查看，不下载

列出每首曲目的类型、权限状态和图片数，不创建输出目录：

```shell
python3 download.py --list
```

只统计下载计划，不创建目录、不写报告、不下载图片：

```shell
python3 download.py --dry-run
```

## 输出结构

每首曲目都有可读且不冲突的目录名：排序号、清洗后的曲名、完整 `post_id`。例如：

```text
output/
├── 十孔口琴/
│   ├── 曲谱/
│   │   └── 002_【十孔口琴】国际歌__18e078b8f16e11eca89352540025c377/
│   │       └── score_01.jpg
│   ├── manifest.json
│   ├── index.tsv
│   ├── failures.tsv
│   └── summary.json
├── 半音阶口琴/
│   ├── 曲谱/
│   ├── manifest.json
│   ├── index.tsv
│   ├── failures.tsv
│   └── summary.json
└── 哨笛/
    ├── 曲谱/
    ├── manifest.json
    ├── index.tsv
    ├── failures.tsv
    └── summary.json
```

文件说明：

- `manifest.json`：专辑、曲目、图片来源、相对路径及最终状态；
- `index.tsv`：适合表格软件查看的曲目索引；
- `failures.tsv`：仅记录真正的图片下载失败；
- `summary.json`：本次运行的汇总统计。

`locked`（当前 token 无权查看）、`no_pics`（图片条目没有图片）和 `non_pic`（例如视频条目）会单独记录在清单和索引中，不会误记为网络失败。

## 曲谱图片识别规则

程序通过 `/api/user/get-album-catalog` 分页读取完整目录，并且只处理 `cate=pic` 的条目。对每首图片曲目，曲谱来源是该曲目内非空 `cover` 与 `pics` 的有序并集：

- 只在同一首曲目内去除完全相同的 URL；
- 不跨曲目去重，因此两个曲目即使引用相同 URL，也会分别保存在各自目录；
- 仍然先检查 `has_right`，无权限的曲目不会下载；
- 非图片条目不会把视频封面误当成曲谱。

实测数据中，半音阶专辑有 3 首曲目的长曲谱图只出现在 `cover`，不能只看 `pics`。按当前专辑内容和本次 token 的权限，十孔专辑为 54 个曲谱文件，半音阶专辑为 83 个，哨笛专辑可下载 355 个（另有 4 个曲谱条目需更高赞助档位）；站点后续增删内容时数量会变化。

客户端也保留了经验证的 `/api/user/get-album-post` 兼容读取逻辑，使用 `lastRank`、`rankOrder=asc`、`rankField=rank`，但正常下载优先使用目录接口，因为它能更可靠地呈现条目类型和 `cover`。

## 断点重跑与可靠性

- 经校验可安全复用的已有文件标记为 `existing`，不会重复下载；
- 只有上次清单的来源 URL 一致、图片签名有效且 SHA-256 校验通过时，才会将旧文件标记为 `existing`；
- 下载先写入同目录的 `.part` 文件，完成后再原子替换正式文件；
- API 和图片下载都支持指数退避重试；
- 多张图片使用线程池并发下载；
- 文件名会清理 `/ \\ : * ? " < > |` 和控制字符，并按 UTF-8 字节安全截断；
- 分页游标或页码若没有前进，程序会停止并报错，避免无限循环。

常用参数：

```shell
python3 download.py --workers 6 --retries 4 --timeout 90
python3 download.py --output /path/to/output
python3 download.py --help
```

## 测试

测试全部使用模拟 API 和模拟图片响应，不需要真实 token，也不会访问网络：

```shell
python3 -m unittest discover -s tests -v
```

测试覆盖会话校验、API 两种分页方式、Cookie 安全、跨源跳转、`cover + pics` 合并、视频跳过、锁定/无图区分、安全文件名、同名曲目、原子写入、重试、幂等重跑及报告生成。


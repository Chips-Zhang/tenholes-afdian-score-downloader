# 曲谱拉取工具

这个仓库收录两个彼此独立的曲谱拉取工具：

| 目录 | 来源网站 | 实现 | 用途 |
| --- | --- | --- | --- |
| [`tenholes-downloader/`](tenholes-downloader/) | 蓝调口琴网（tenholes.com） | Bash、curl、Ruby 标准库 | 拉取有权访问的曲谱图片、示范音频和伴奏 |
| [`afdian-downloader/`](afdian-downloader/) | 爱发电（afdian.com） | Python 标准库 | 拉取有权访问的图片专辑曲谱 |

## 使用边界

本项目只用于备份或整理你已经购买、获授权访问且依法可以留存的内容。请遵守网站服务条款、版权要求及所在地法律，并控制请求频率。工具不会也不应被用于绕过登录、付费、权限或其他访问控制。

源码采用 Apache License 2.0；通过这些工具取得的网页、图片、音频等内容仍归各自权利人所有，不因源码许可证而改变授权状态。

## 快速开始

### 蓝调口琴网

需要 Bash、curl、Ruby 和支持 `-P` 的 `xargs`。进入工具目录后运行：

```shell
cd tenholes-downloader
bash download_new_first_3.sh
```

脚本会隐藏提示输入当前登录会话的 `ten_auth1` Cookie 值；输入不会回显，也不会写入配置文件。确认前三页流程符合预期后，可拉取完整列表：

```shell
bash download_all.sh
```

详细说明见 [`tenholes-downloader/README.md`](tenholes-downloader/README.md)。

### 爱发电

需要 Python 3.9 或更高版本，不需要安装第三方依赖：

```shell
cd afdian-downloader
python3 download.py --dry-run
python3 download.py
```

未设置 `AFDIAN_AUTH_TOKEN` 时，程序会隐藏提示输入 token。建议优先使用这种方式，避免凭据进入 shell 历史。专辑选择、输出结构、断点重跑及更多参数见 [`afdian-downloader/README.md`](afdian-downloader/README.md)。

## 输出与凭据

- 下载结果默认写入各工具目录下的 `output/`，该目录已被 Git 忽略。
- 不要把 Cookie、token、`.env`、日志、网页缓存或下载内容提交到仓库。
- 不要在 Issue、截图、终端录屏或 CI 日志中公开凭据。
- 蓝调口琴网脚本当前访问的是站点提供的 HTTP 地址，Cookie 在传输链路上无法获得 HTTPS 的保护；仅应在可信网络中使用，并在使用后及时使会话失效。
- 更完整的凭据处理和泄露应对建议见 [`SECURITY.md`](SECURITY.md)。

## 测试

爱发电测试完全使用模拟响应，不需要真实 token，也不会访问网络：

```shell
cd afdian-downloader
python3 -m unittest discover -s tests -v
```

蓝调口琴网脚本可做离线语法检查：

```shell
bash -n tenholes-downloader/download_all.sh
bash -n tenholes-downloader/download_new_first_3.sh
```

GitHub Actions 会在每次推送和拉取请求中执行以上检查。

## 许可证

项目源码按 [Apache License 2.0](LICENSE) 发布。

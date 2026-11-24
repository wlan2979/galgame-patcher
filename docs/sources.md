# 补丁来源与使用条款

`galpatch search` 从公开来源查找候选链接。`galpatch sites` 同时列出可直接在浏览器查阅的网站。页面、费用和兼容信息以各发布者的当前说明为准。

| 来源 | 类型 | 价格与再分发 |
| --- | --- | --- |
| [柚子社支持页](https://www.yuzu-soft.com/support.html) | 发行商更新与支持说明 | 各作品页面单独说明下载和使用条件。镜像前应核对权利人授权。 |
| [NekoNyan](https://patches.nekonyansoft.com/) · [Kagura Games](https://www.kaguragames.com/patch/) | 发行商补丁目录 | 费用与适用商店见作品卡片。免费提供下载的补丁也有各自的分发条款。 |
| [Shiravune](https://shiravune.com/) · [Sekai Project 支持中心](https://support.sekaiproject.com/portal/en/kb) | 发行商官网或支持入口 | 按作品查找公告与商店页面，核对价格、地区和授权。 |
| [VNDB](https://vndb.org/r) · [PCGamingWiki](https://www.pcgamingwiki.com/) | 社区索引或百科 | 外链由不同发布者提供，费用与许可逐条核对。 |
| [07th-Mod](https://07th-mod.com/) · [GitHub Releases](https://github.com/) | 社区项目和发布平台 | 逐项核对代码、压缩包和其中素材的许可。07th-Mod 的语音与立绘应按相应发布条款处理。 |
| [2DFan 公开入口](https://github.com/2dfan/domains) | 社区导航，由浏览器访问 | 按游戏名查阅作品帖和补丁说明，检查发布者、适用版本与授权。 |

发布者允许下载、游戏许可允许修改、权利人允许再分发是独立的授权。提交来源链接时，附上发布者、适用游戏版本和发布说明页。允许镜像或再上传时，附上相应授权的链接。

## 自定义来源

运行 `galpatch sites --init-config` 显示并初始化 `sources.json` 的位置。Windows 默认为 `%APPDATA%\galgame-patcher\sources.json`，未设置 `APPDATA` 时为 `~/.config/galgame-patcher/sources.json`。参考[社区来源示例](community-sources.example.json)，把你关注的公开发布页 URL 加入其中的 `extra_pages`，游戏目录页和作品专帖通常更容易匹配。

```json
{
  "version": 1,
  "extra_pages": [],
  "trusted_domains": [],
  "trusted_github_owners": []
}
```

白名单用于标注来源。文件完整性需用来自可信渠道的预期 SHA256 核对。需要登录的论坛、提取码网盘和脚本生成的下载页，请在浏览器按发布者的说明操作。

自动搜索按站点间隔发起请求，检查公开网页的 `robots.txt`，暂时性错误最多请求三次并尊重 `Retry-After`。遇到受限站点时，可用 `galpatch search --verbose "游戏名"` 查看原因，再用 `galpatch sites` 在浏览器打开入口。

Kagura Games 目录受限时，搜索仍可给出少量已核对的发行商发布页，例如 Summer Memories 本体及 Expansion DLC。搜索时可输入发行页使用的游戏名称。结果为手动安装，商店、语言和 DLC 需逐项匹配。来源覆盖取决于内置站点和你订阅的发布页。

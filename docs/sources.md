# 补丁来源与使用条款

`galpatch search` 从公开来源查找候选链接。`galpatch sites` 同时列出可直接在浏览器查阅的网站。页面、费用和兼容信息以各发布者的当前说明为准。

| 来源 | 类型 | 价格与再分发 |
| --- | --- | --- |
| [柚子社支持页](https://www.yuzu-soft.com/support.html) | 发行商更新与支持说明 | 各作品页面单独说明下载和使用条件。镜像前应核对权利人授权。 |
| [NekoNyan](https://patches.nekonyansoft.com/) · [Kagura Games](https://www.kaguragames.com/patch/) | 发行商补丁目录 | 费用与适用商店见作品卡片。免费提供下载的补丁也有各自的分发条款。 |
| [Shiravune](https://shiravune.com/) · [Sekai Project 支持中心](https://support.sekaiproject.com/portal/en/kb) | 发行商官网或支持入口 | 按作品查找公告与商店页面，核对价格、地区和授权。 |
| [VNDB](https://vndb.org/r) · [PCGamingWiki](https://www.pcgamingwiki.com/) | 社区索引或百科 | 外链由不同发布者提供，费用与许可逐条核对。 |
| [07th-Mod](https://07th-mod.com/) · [GitHub Releases](https://github.com/) | 社区项目和发布平台 | 逐项核对代码、压缩包和其中素材的许可。07th-Mod 的语音与立绘应按相应发布条款处理。 |

发布者允许下载、游戏许可允许修改、权利人允许再分发是独立的授权。提交来源链接时，附上发布者、适用游戏版本和发布说明页。允许镜像或再上传时，附上相应授权的链接。

## 自定义来源

运行 `galpatch sites --init-config` 显示并初始化 `sources.json` 的位置。Windows 默认为 `%APPDATA%\galgame-patcher\sources.json`，未设置 `APPDATA` 时为 `~/.config/galgame-patcher/sources.json`。将实际站点加入模板中的相应字段。

```json
{
  "version": 1,
  "extra_pages": [],
  "trusted_domains": [],
  "trusted_github_owners": []
}
```

白名单用于标注来源。文件完整性需用来自可信渠道的预期 SHA256 核对。遇到自动请求受限的站点，可从 `galpatch sites` 在浏览器打开。

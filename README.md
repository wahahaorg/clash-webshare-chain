# clash-webshare-chain

在 macOS + Clash Verge 上，自动筛选「能穿透 Webshare 静态住宅代理」的机场出口，并生成全局 `Script.js` 里的上游分组。

解决的具体问题：Webshare 静态住宅代理只接受来自**特定机场出口 IP** 的连接。其他节点 TCP 能握手，但认证后的 CONNECT 会挂住（约 5 秒后 TLS 报错）。所以「TCP 能连通」并不等于「能用」——必须做端到端测试。这个工具就是把这套测试和配置生成自动化。

## 它做什么

`run` 一条命令完成整个循环：

1. 把当前订阅的**所有**真实节点临时写进 `Webshare · 上游` 分组（候选模式）
2. 重启 Clash Verge，让配置生效
3. 逐个选择节点，通过真实链路 `节点 → Webshare → Google API` 探测
4. 只保留通过的节点，按实测延迟排序，重新生成 `Script.js`
5. 再重启一次，复核最优节点，输出结果

只对 Antigravity 的三个域名生效，不影响其他流量的分流：

- `generativelanguage.googleapis.com`
- `daily-cloudcode-pa.googleapis.com`
- `cloudcode-pa.googleapis.com`

## 环境要求

- macOS，Clash Verge **service 模式**（默认的 TUN 模式即可）
- Python 3.9+，`curl`
- 可选：Node.js（生成 `Script.js` 后用 `node --check` 做语法校验）
- 一个 Webshare 静态住宅代理账号（HTTP proxy，用户名/密码认证）

## 快速开始

```bash
git clone <repo-url> clash-webshare-chain
cd clash-webshare-chain

mkdir -p ~/.config/clash-webshare-chain
cp config.example.json ~/.config/clash-webshare-chain/config.json
$EDITOR ~/.config/clash-webshare-chain/config.json
```

至少要改这几项：

| 字段 | 说明 |
| --- | --- |
| `webshare.server` / `port` | Webshare 代理地址和端口 |
| `webshare.username` / `password` | Webshare 账号 |
| `mihomo_socket` | Clash Verge 的 mihomo unix socket；路径里的 uid 不是 501 时要改（找不到时工具会尝试自动探测） |
| `mihomo_secret` | Clash Verge 外部控制密钥 |
| `script_path` / `profiles_yaml` | Clash Verge 配置目录；默认是 macOS 常见路径 |

先确认能读到节点：

```bash
python3 clash_webshare_chain.py nodes
```

只测当前分组，不改配置：

```bash
python3 clash_webshare_chain.py probe
```

完整跑一遍（会重写 `Script.js` 并重启 Clash Verge 两次，网络会短暂中断）：

```bash
python3 clash_webshare_chain.py run --yes
```

运行记录写在 `~/.config/clash-webshare-chain/last-run.json`，每个订阅的可用节点列表持久化在同目录 `state.json`。

## 命令

| 命令 | 作用 |
| --- | --- |
| `nodes [--json]` | 列出所有分组和真实叶子节点 |
| `probe [--limit N] [--json FILE]` | 探测当前 `Webshare · 上游` 分组里的节点，不修改配置 |
| `render [--out FILE]` | 只用 `state.json` 里的结果重新生成 `Script.js`，不重启 |
| `run [--yes] [--profile NAME]` | 全流程：候选 → 探测 → 生成 → 重启复核 |

`--profile` 一般不用传：工具会从 `profiles.yaml` 的 `current` 找到当前订阅名。

## 结果怎么判断

探测命令等价于：

```bash
curl -m 10 -x http://127.0.0.1:7897 \
  https://generativelanguage.googleapis.com/v1beta/models
```

- 返回 `200/401/403/404` → 链路通了（403 只是没带 API key 的正常拒绝）
- 约 5 秒后失败、`000`、TLS error → 该出口被 Webshare 拒绝
- `407` → Webshare 认证失败，检查账号

失败节点重试 `attempts` 轮后才剔除，默认 2 轮，减少网络抖动导致的误杀。

## 生成的分组长什么样

`Script.js` 模板里的核心逻辑：

- `PROFILE_NODES` 按订阅名保存实测可用节点，顺序即延迟顺序，第一个是默认出口
- 切换订阅后，只保留当前订阅里真实存在的节点；匹配不到时退回 `FALLBACK_PATTERNS`（默认收录了赔钱机场、魔界的旧实测结果）
- `Webshare · Static Residential` 是注入的 HTTP 代理，`dialer-proxy` 指向上游分组
- 旧版本注入的 `Webshare · 上游(美国)` 会被自动清理

注意：这个工具会**整体重写** `Script.js`（生成前自动备份，保留最近 20 份）。如果你在 `Script.js` 里还有其他自定义逻辑，请把它合并进 `script_template.js`，否则下次运行会被覆盖。

## 常见问题

**分组选项数不对 / 一直是旧配置**
Clash Verge 只有重启后才会重新执行 `Script.js`。工具会检查重启后分组选项数是否等于预期，不等就报错退出，不会拿旧配置继续测。

**重启后默认节点不是最快的**
Clash Verge 的 API 切换不一定落盘，重启后会恢复 `profiles.yaml` 里保存的选择。只要那个节点还在本次验证出的列表里就没问题（列表里的每个节点都实测通过）；如果旧选择已不在列表，Clash Verge 会回退到组内第一项，也就是工具按延迟排序后的最快节点。想强制换默认，可在 GUI 里手动点一次，或退出 Clash Verge 后编辑 `profiles.yaml` 的对应 `selected`。

**订阅更新后节点改名了**
机场经常改名（例如新增/删除节点）。重新跑一次 `run` 即可，候选集合是按当前订阅实时读取的，不依赖写死的节点名。

**没有 `node` 命令**
跳过语法校验，不影响使用。

**Windows / Linux**
工具本身只用 Python 标准库，但默认路径、`osascript`、`open -a` 是 macOS 的。其他平台改 `mihomo_url`（TCP 外部控制端口）、`script_path`、`quit_cmd` / `start_cmd` 即可，未做测试。

## 安全

- Webshare 用户名/密码只存在于本机 `~/.config/clash-webshare-chain/config.json`，仓库里的 `config.example.json` 只有占位符
- `state.json`、`last-run.json`、`Script.js` 备份都在 `.gitignore` 里
- 生成的 `Script.js` 会包含 Webshare 凭据（Clash Verge 的脚本无法读取外部文件），注意不要把这个文件提交到任何仓库

## License

MIT

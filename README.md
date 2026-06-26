# Dota2 Team ID Fix

用于从 StarRocks 读取 Dota2 数据，检测同样 5 个选手在同一联赛或跨联赛中对应不同 Team ID 的情况，并在本地网页中查看、筛选和维护队伍映射 CSV。

## 安装依赖

```powershell
python -m pip install -r requirements.txt
```

## 启动网页

不要在代码中写入数据库密码。先设置环境变量：

```powershell
$env:STARROCKS_PASSWORD="your_password"
python .\dota_roster_web.py
```

然后打开：

```text
http://127.0.0.1:8000/
```

## 页面说明（4 个标签页）

1. **查询数据**：从数据库直接生成异常结果，或导入已有 CSV。
2. **结果分析**：统计卡片、Top 联赛图表、卡片/表格视图（表格支持排序、分页、`match_ids` 折叠、时间戳转可读日期）。
3. **维护表**：在网页内可编辑的队伍映射 CSV，按“组”分组展示。
4. **选手追踪**：按 steamid 或选手名搜索，查看该选手代表过哪些队伍、打过哪些联赛。

## 主要功能

- 从 `dota2_stats.players` 和 `dota2_stats.match_info` 读取完整数据，并关联 `pro_players`、`pro_match_list*` 等表补全选手名 / 联赛名。
- 同一联赛内同 5 人多 Team ID 检测。
- 跨联赛同 5 人不同 Team ID 检测。
- **阵容模糊匹配阈值**：可选“完全相同 / 允许 1 人不同 / 允许 2 人不同”，用于识别换人后仍是同一支队伍的多个 Team ID（脚本参数 `--max-diff 0|1|2`）。
- 按比赛结束时间范围筛选。
- **维护表**：可编辑 CSV，字段为 `group_id, roster, league_id, league_name, team_id, team_name, note`，同一支真实队伍的多个 Team ID 共享组号并按组高亮展示，便于看出关联关系。
- **选手追踪**：输入 steamid（纯数字）直接追踪；输入名字会先列出候选选手再选择。结果含「代表过的队伍」与「参加过的联赛」两张表。

## 命令行用法（可选，不经网页）

```powershell
# 跨联赛、允许 1 人不同的检测，导出 CSV
python .\detect_same_roster_team_ids.py --detection-mode cross_league --max-diff 1 --yes --output result.csv
```

## 本地 API

- `GET  /api/candidates`：自动发现候选表。
- `POST /api/detect`：运行异常检测（参数含 `detection_mode`、`max_diff`、时间范围、`league_id`、`limit`）。
- `GET  /api/manual-records` / `POST /api/manual-records/save-all` / `POST /api/manual-records/delete`：维护表读写。
- `GET  /api/player-candidates?q=名字`：按名字搜索选手。
- `POST /api/player-track`：按 steamid 追踪队伍 / 联赛历史。

## 注意事项

- 必须用 `python dota_roster_web.py` 启动后访问网页；直接双击 HTML 无法使用数据库与保存功能。
- 修改 `.py` 代码后需重启服务；修改 HTML 刷新浏览器即可。
- 保存维护表前，请先在 Excel / WPS 里关闭 `manual_team_id_records.csv`，否则文件被锁无法写入。
- 数据库密码只通过 `STARROCKS_PASSWORD` 环境变量传入；生成的 CSV、`.env`、日志均不提交到 Git。

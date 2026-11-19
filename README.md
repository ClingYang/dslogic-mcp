# DSLogic MCP

DSLogic MCP 让 AI 客户端通过 MCP 接管 DSLogic U2Pro16 的采集流程。它使用 DSView 底层驱动直接连接分析仪，可以探测设备、启动和停止采集、查询状态，并将波形保存为 DSView 可打开的 `.dsl` 文件。服务器以无界面方式运行，开始采集前请退出 DSView 桌面程序，释放 USB 设备。

24C08 I²C 波形还原与 BIN 比对是附带工具。

## 安装

硬件采集以 Windows x64、Python 3.13 和 DSLogic U2Pro16 为验证配置。先安装设备的 WinUSB 驱动。采集前退出 DSView 或其他正在占用分析仪的程序。驱动接口由 [pydsview](https://pypi.org/project/pydsview/0.1.7/) 提供。

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe dslogic_mcp.py probe
```

## 连接 MCP 客户端

服务器使用标准输入输出。将以下路径改成你实际解压或克隆的位置。输出目录默认位于源码旁的 `captures/`，也可用 `--data-dir` 指定。

```json
{
  "mcpServers": {
    "dslogic": {
      "command": "C:/path/to/dslogic-mcp/.venv/Scripts/python.exe",
      "args": ["C:/path/to/dslogic-mcp/dslogic_mcp.py", "serve"]
    }
  }
}
```

| 工具 | 用途 |
| --- | --- |
| `probe_device` | 打开 U2Pro16 并返回型号和通道，随后释放 USB |
| `start_capture` | 启动 2–45 秒被动采集。固定 2 MHz、0.9 V 数字阈值，使用全部数字通道 |
| `capture_status` | 查看采集状态或已保存的文件名 |
| `finish_capture` | 提前结束、保存波形并释放设备 |
| `compare_24c08` | 以波形文件名 `waveform` 和 BIN 文件名 `baseline` 为输入，比较 1024 字节。两个文件均放在输出目录，可指定 `scl` 和 `sda` 通道号 |

采集时连接 SCL、SDA 和 GND，待 `start_capture` 返回 `capturing` 再运行待观察的 I²C 操作。工具只采样数字信号。芯片供电由被测设备提供。

## 离线比对

比对脚本可单独运行，支持 DSView `.dsl` 和具有相同逻辑数据目录结构的 PXView `.pxl`。下例把 SCL 接到通道 3，SDA 接到通道 1。省略通道号时，程序尝试识别通道。当覆盖不完整时返回 `partial`。有多个可用通道组合时返回 `ambiguous`。只有完整、无冲突的结果才可导出为 BIN。

```powershell
python compare_24c08_capture.py captures/example.dsl captures/baseline.bin --scl 3 --sda 1
```

## 许可证

[GPL-3.0-or-later](LICENSE)。

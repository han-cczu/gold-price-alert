"""CLI 命令行入口 - 纯 Web 模式"""

import argparse

from gold_monitor import __version__


def main():
    """CLI 入口 - 启动 Web 服务"""
    parser = argparse.ArgumentParser(
        description="金价实时监控系统",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  gold-monitor                    # 使用默认配置启动
  gold-monitor --port 8080        # 指定端口
  gold-monitor --host 127.0.0.1   # 仅本地访问

环境变量配置:
  GOLD_DATA_SOURCE       数据源 (mock/sina/goldapi)
  GOLD_FETCH_INTERVAL    采集间隔秒数 (默认 30)
  GOLD_LLM_PROVIDER      大模型 (anthropic/openai/mock)
        """,
    )

    parser.add_argument(
        "--host", type=str, default="0.0.0.0", help="监听地址 (默认: 0.0.0.0)"
    )
    parser.add_argument("--port", type=int, default=8000, help="监听端口 (默认: 8000)")
    parser.add_argument(
        "--reload", action="store_true", help="开发模式，文件变更自动重载"
    )
    parser.add_argument(
        "--version", action="version", version=f"gold-monitor {__version__}"
    )

    args = parser.parse_args()

    # 启动 Web 服务
    run_server(args.host, args.port, args.reload)


def run_server(host: str = "0.0.0.0", port: int = 8000, reload: bool = False):
    """启动 Web 服务"""
    import uvicorn

    print(f"""
╔══════════════════════════════════════════════════════════════╗
║              金价实时监控系统 v{__version__}                          ║
╠══════════════════════════════════════════════════════════════╣
║  访问地址: http://{host}:{port:<5}                              ║
║  健康检查: http://{host}:{port:<5}/health                       ║
║  API 文档: http://{host}:{port:<5}/docs                         ║
╠══════════════════════════════════════════════════════════════╣
║  数据采集: 自动运行                                           ║
║  告警检测: 自动运行                                           ║
╚══════════════════════════════════════════════════════════════╝
    """)

    uvicorn.run(
        "gold_monitor.web:app", host=host, port=port, reload=reload, log_level="info"
    )


if __name__ == "__main__":
    main()

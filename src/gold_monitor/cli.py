"""CLI 命令行界面"""

import asyncio
import sys
from datetime import datetime, timedelta

from rich.console import Console
from rich.table import Table
from rich.live import Live
from rich.panel import Panel
from rich.layout import Layout
from rich.text import Text
from rich.markdown import Markdown

from .config import settings
from .models import Database
from .collector import DataCollector, create_data_source
from .data_sources.base import PriceData
from .alert import AlertMonitor, ConsoleNotification
from .analyzer import GoldAnalyzer


console = Console()


def create_price_panel(price_data: PriceData | None, prev_price: float | None = None) -> Panel:
    """创建价格显示面板"""
    if price_data is None:
        return Panel("等待数据...", title="当前金价", border_style="dim")

    # 计算涨跌
    if prev_price and prev_price > 0:
        change = price_data.price - prev_price
        change_pct = (change / prev_price) * 100
        if change >= 0:
            change_text = f"[green]+{change:.2f} (+{change_pct:.2f}%)[/green]"
            arrow = "▲"
        else:
            change_text = f"[red]{change:.2f} ({change_pct:.2f}%)[/red]"
            arrow = "▼"
    else:
        change_text = ""
        arrow = ""

    content = Text()
    content.append(f"${price_data.price:.2f}", style="bold yellow")
    content.append(f" {arrow} ", style="bold")
    if change_text:
        content.append_text(Text.from_markup(change_text))
    content.append(f"\n\n数据源: {price_data.source}")
    content.append(f"\n更新时间: {price_data.timestamp.strftime('%Y-%m-%d %H:%M:%S')}")

    return Panel(content, title="XAU/USD 金价", border_style="yellow")


def create_history_table(db: Database, limit: int = 10) -> Table:
    """创建历史价格表格"""
    table = Table(title="最近价格记录")
    table.add_column("时间", style="cyan")
    table.add_column("价格 (USD)", style="yellow", justify="right")
    table.add_column("来源", style="dim")

    records = db.get_recent_prices(limit)
    for record in records:
        table.add_row(
            record.timestamp.strftime("%H:%M:%S"),
            f"${record.price:.2f}",
            record.source
        )

    return table


async def run_monitor():
    """运行实时监控"""
    # 初始化数据库
    db = Database(settings.database_url)
    db.create_tables()

    # 创建告警监控器
    alert_monitor = AlertMonitor(
        database=db,
        channels=[ConsoleNotification()],
        threshold_upper=settings.alert_price_upper,
        threshold_lower=settings.alert_price_lower,
        volatility_percent=settings.alert_threshold_percent,
        volatility_window_minutes=settings.alert_volatility_window
    )

    # 价格更新回调
    async def on_price_update(price_data: PriceData):
        await alert_monitor.check_price(price_data)

    # 创建数据采集器
    collector = DataCollector(db, on_price_update=lambda p: asyncio.create_task(on_price_update(p)))

    console.print("[bold green]金价实时监控系统启动[/bold green]")
    console.print(f"数据源: {settings.data_source}")
    console.print(f"采集间隔: {settings.fetch_interval}秒")
    console.print(f"告警阈值: 上限 ${settings.alert_price_upper:.2f} | 下限 ${settings.alert_price_lower:.2f} | 波动 {settings.alert_threshold_percent}%")
    console.print("按 Ctrl+C 退出\n")

    prev_price = None

    try:
        # 先采集一次
        price_data = await collector.fetch_once()
        prev_price = price_data.price

        # 启动定时采集
        collector.start()

        # 实时显示
        with Live(console=console, refresh_per_second=1) as live:
            while True:
                layout = Layout()
                layout.split_column(
                    Layout(create_price_panel(collector.last_price, prev_price), name="price"),
                    Layout(create_history_table(db), name="history")
                )
                live.update(layout)

                if collector.last_price:
                    prev_price = collector.last_price.price

                await asyncio.sleep(1)

    except KeyboardInterrupt:
        console.print("\n[yellow]正在停止...[/yellow]")
    finally:
        collector.stop()
        console.print("[green]已退出[/green]")


def show_history(hours: int = 24):
    """显示历史数据"""
    db = Database(settings.database_url)

    end_time = datetime.utcnow()
    start_time = end_time - timedelta(hours=hours)

    records = db.get_prices_in_range(start_time, end_time)

    if not records:
        console.print("[yellow]没有找到历史数据[/yellow]")
        return

    table = Table(title=f"最近 {hours} 小时金价记录")
    table.add_column("时间", style="cyan")
    table.add_column("价格 (USD)", style="yellow", justify="right")
    table.add_column("来源", style="dim")

    for record in records:
        table.add_row(
            record.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            f"${record.price:.2f}",
            record.source
        )

    console.print(table)

    # 统计信息
    prices = [r.price for r in records]
    console.print(f"\n统计: 最高 ${max(prices):.2f} | 最低 ${min(prices):.2f} | 平均 ${sum(prices)/len(prices):.2f}")


def main():
    """CLI 入口"""
    import argparse

    parser = argparse.ArgumentParser(description="金价实时监控系统")
    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    # monitor 命令
    monitor_parser = subparsers.add_parser("monitor", help="启动实时监控")

    # history 命令
    history_parser = subparsers.add_parser("history", help="查看历史数据")
    history_parser.add_argument("--hours", type=int, default=24, help="查询小时数")

    # fetch 命令
    fetch_parser = subparsers.add_parser("fetch", help="获取一次当前金价")

    # alerts 命令
    alerts_parser = subparsers.add_parser("alerts", help="查看告警历史")
    alerts_parser.add_argument("--limit", type=int, default=20, help="显示条数")

    # analyze 命令
    analyze_parser = subparsers.add_parser("analyze", help="分析当前金价波动")

    # web 命令
    web_parser = subparsers.add_parser("web", help="启动 Web 服务")
    web_parser.add_argument("--host", type=str, default="0.0.0.0", help="监听地址")
    web_parser.add_argument("--port", type=int, default=8000, help="监听端口")

    args = parser.parse_args()

    if args.command == "monitor":
        asyncio.run(run_monitor())
    elif args.command == "history":
        show_history(args.hours)
    elif args.command == "fetch":
        asyncio.run(fetch_once())
    elif args.command == "alerts":
        show_alerts(args.limit)
    elif args.command == "analyze":
        asyncio.run(run_analysis())
    elif args.command == "web":
        run_web_server(args.host, args.port)
    else:
        # 默认启动监控
        asyncio.run(run_monitor())


async def fetch_once():
    """获取一次金价"""
    db = Database(settings.database_url)
    db.create_tables()

    source = create_data_source()
    price_data = await source.fetch_price()

    db.save_price(price_data.price, price_data.source)

    console.print(f"[yellow]金价: ${price_data.price:.2f}[/yellow]")
    console.print(f"数据源: {price_data.source}")
    console.print(f"时间: {price_data.timestamp}")


def show_alerts(limit: int = 20):
    """显示告警历史"""
    db = Database(settings.database_url)

    from .models import AlertRecord
    with db.get_session() as session:
        records = session.query(AlertRecord).order_by(
            AlertRecord.triggered_at.desc()
        ).limit(limit).all()

    if not records:
        console.print("[yellow]没有告警记录[/yellow]")
        return

    table = Table(title="告警历史")
    table.add_column("时间", style="cyan")
    table.add_column("类型", style="yellow")
    table.add_column("价格", style="green", justify="right")
    table.add_column("消息", style="white")

    for record in records:
        table.add_row(
            record.triggered_at.strftime("%Y-%m-%d %H:%M:%S"),
            record.alert_type,
            f"${record.price:.2f}",
            record.message
        )

    console.print(table)


async def run_analysis():
    """运行金价分析"""
    db = Database(settings.database_url)

    # 获取最近的价格数据
    records = db.get_recent_prices(limit=20)
    if len(records) < 2:
        console.print("[yellow]数据不足，无法进行分析。请先运行 monitor 采集数据。[/yellow]")
        return

    # 准备分析数据
    current_price = records[0].price
    oldest_price = records[-1].price
    price_change = current_price - oldest_price

    recent_prices = [(r.timestamp, r.price) for r in reversed(records)]

    console.print("[bold]正在分析金价波动...[/bold]\n")

    analyzer = GoldAnalyzer()
    try:
        report = await analyzer.analyze_volatility(
            current_price=current_price,
            price_change=price_change,
            recent_prices=recent_prices,
            time_window_minutes=settings.alert_volatility_window
        )

        # 显示报告（使用 Panel 替代 Markdown 避免编码问题）
        report_text = analyzer.format_report_markdown(report)
        console.print(Panel(report_text, title="分析报告", border_style="green"))

    except Exception as e:
        console.print(f"[red]分析失败: {e}[/red]")


def run_web_server(host: str = "0.0.0.0", port: int = 8000):
    """启动 Web 服务"""
    console.print(f"[bold green]启动 Web 服务...[/bold green]")
    console.print(f"访问地址: http://{host}:{port}")
    console.print("按 Ctrl+C 退出\n")

    from .web import run_server
    run_server(host, port)


if __name__ == "__main__":
    main()

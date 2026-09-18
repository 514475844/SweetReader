from app import create_app

app = create_app()

if __name__ == '__main__':
    # [B3] waitress 取代 Flask dev server：多线程，扫描期间站点不再卡死。
    # 万一 waitress 没装上，退回 threaded 模式的 dev server，不至于起不来。
    try:
        from waitress import serve
        # 容器里 stdout 是块缓冲，不加 flush 日志要攒满 4KB 才出现
        print('[run] starting with waitress on 0.0.0.0:5000 (threads=8)', flush=True)
        serve(app, host='0.0.0.0', port=5000, threads=8)
    except ImportError:
        print('[run] waitress not available, fallback to Flask dev server', flush=True)
        app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)

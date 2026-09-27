from app import create_app

app = create_app()

if __name__ == '__main__':
    # [B3] waitress 取代 Flask dev server：多线程，扫描期间站点不再卡死。
    # 万一 waitress 没装上，退回 threaded 模式的 dev server，不至于起不来。
    try:
        from waitress import serve
        # 容器里 stdout 是块缓冲，不加 flush 日志要攒满 4KB 才出现
        print('[run] starting with waitress on 0.0.0.0:5000 (threads=8)', flush=True)
        # waitress 默认 max_request_body_size=1GB，超大压缩包（8~14G）会在它那里被
        # 直接掐断（浏览器只见「网络错误」）——放宽到 18GB，与 Flask 层 16GB 上限配套。
        serve(app, host='0.0.0.0', port=5000, threads=8,
              max_request_body_size=18 * 1024 * 1024 * 1024)
    except ImportError:
        print('[run] waitress not available, fallback to Flask dev server', flush=True)
        app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)

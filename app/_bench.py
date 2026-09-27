# -*- coding: utf-8 -*-
"""量 /api/read/<id>（大 txt）的耗时：改前 / 改后对比。"""
import os
import sys
import time

sys.path.insert(0, '/app')
from app import create_app  # noqa: E402
from app.models import User  # noqa: E402

app = create_app()
IDS = [int(x) for x in (sys.argv[1:] or ['2079'])]

with app.app_context():
    u = User.query.filter_by(is_admin=True, is_active=True).first()
    uid = str(u.id)

with app.test_client() as c:
    with c.session_transaction() as s:
        s['_user_id'] = uid
        s['_fresh'] = True
    for bid in IDS:
        for i in (1, 2):
            t0 = time.time()
            r = c.get('/api/read/%d' % bid)
            dt = time.time() - t0
            n = len(r.get_data())
            print('READ id=%d run=%d %.2fs bytes=%d status=%d' % (bid, i, dt, n, r.status_code))

#!/bin/bash

echo "🔄 重启 SweetReader..."
docker restart sweetreader
sleep 2
docker logs sweetreader --tail 10
echo ""
echo "🍭 重启完成！"

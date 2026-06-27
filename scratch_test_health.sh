python3 -m aml.cloud.erasure_daemon &
DAEMON_PID=$!
sleep 3
curl -s http://localhost:9091/health
kill $DAEMON_PID

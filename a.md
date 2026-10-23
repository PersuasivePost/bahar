What	Command
Start	~/start-bahar.sh
Restart (after code changes)	tmux kill-session -t bahar; ~/start-bahar.sh
See the log	tail -f ~/bahar-server.log
Tunnel (backup)	cloudflared tunnel --url http://localhost:7860
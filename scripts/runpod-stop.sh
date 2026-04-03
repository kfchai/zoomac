#!/bin/bash
# Stop Ollama on RunPod to free GPU memory
# Usage: bash scripts/runpod-stop.sh

HOST="root@213.173.111.17"
PORT="27796"
KEY="~/.ssh/iid_runpod"
SSH="ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=no $HOST -p $PORT -i $KEY"

echo "🔍 Connecting to RunPod..."
if ! $SSH "echo ok" 2>/dev/null; then
    echo "❌ Cannot connect. Pod might already be stopped."
    exit 0
fi

echo "🛑 Stopping Ollama..."
$SSH "pkill ollama 2>/dev/null; sleep 1; pkill -9 ollama 2>/dev/null"

# Verify
if $SSH "pgrep ollama" 2>/dev/null; then
    echo "⚠️  Ollama still running"
else
    echo "✅ Ollama stopped"
fi

# Show GPU is free
echo ""
$SSH "nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv,noheader" 2>/dev/null
echo ""
echo "💡 To also stop the pod (save $$$), go to RunPod dashboard and stop it."
echo "   The model is saved in /workspace — it persists across restarts."

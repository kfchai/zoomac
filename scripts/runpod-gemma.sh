#!/bin/bash
# Connect to RunPod Gemma 4 26B — auto-starts Ollama if needed
# Usage: bash scripts/runpod-gemma.sh

HOST="root@213.173.111.17"
PORT="27796"
KEY="~/.ssh/iid_runpod"
SSH="ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=no $HOST -p $PORT -i $KEY"

echo "🔍 Checking RunPod connection..."
if ! $SSH "echo ok" 2>/dev/null; then
    echo "❌ Cannot connect to RunPod. Is the pod running?"
    exit 1
fi
echo "✅ Connected to RunPod"

echo "🔍 Checking if Ollama is running..."
OLLAMA_STATUS=$($SSH "curl -s http://localhost:11434/api/tags 2>/dev/null")

if echo "$OLLAMA_STATUS" | grep -q "gemma4"; then
    echo "✅ Gemma 4 26B is already loaded"
else
    echo "🚀 Starting Ollama..."
    $SSH "export OLLAMA_MODELS=/workspace/ollama_models && export OLLAMA_HOST=0.0.0.0 && pkill ollama 2>/dev/null; sleep 1 && nohup ollama serve > /workspace/ollama.log 2>&1 &"
    echo "⏳ Waiting for Ollama to start..."
    sleep 3

    # Verify
    RETRY=0
    while [ $RETRY -lt 10 ]; do
        if $SSH "curl -s http://localhost:11434/api/tags 2>/dev/null" | grep -q "models"; then
            echo "✅ Ollama is running"
            break
        fi
        RETRY=$((RETRY + 1))
        echo "   Waiting... ($RETRY/10)"
        sleep 2
    done

    if [ $RETRY -eq 10 ]; then
        echo "❌ Ollama failed to start. Check /workspace/ollama.log"
        exit 1
    fi
fi

# Check GPU
echo ""
$SSH "nvidia-smi --query-gpu=name,memory.used,memory.total,temperature.gpu --format=csv,noheader" 2>/dev/null
echo ""

echo "🔗 Starting SSH tunnel: localhost:11434 → RunPod Ollama"
echo "   Use in Zoomac: provider=ollama, model=gemma4:26b, baseUrl=http://localhost:11434/v1"
echo ""
echo "   Press Ctrl+C to disconnect"
echo ""

# Start tunnel (foreground — blocks until Ctrl+C)
ssh -L 11434:localhost:11434 $HOST -p $PORT -i $KEY -N

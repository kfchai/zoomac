#!/bin/bash
# Connect to RunPod vLLM — auto-starts if needed
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

echo "🔍 Checking if vLLM is running..."
if $SSH "curl -s --max-time 3 http://localhost:8000/v1/models 2>/dev/null" | grep -q "gemma"; then
    echo "✅ vLLM is already running"
    $SSH "curl -s http://localhost:8000/v1/models 2>/dev/null | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d[\"data\"][0][\"id\"])' 2>/dev/null"
else
    echo "🚀 Starting vLLM..."
    $SSH "nohup /workspace/start_vllm.sh >/workspace/vllm.log 2>&1 &"

    echo "⏳ Waiting for vLLM (model loading takes a few minutes)..."
    for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
        sleep 10
        if $SSH "curl -s --max-time 3 http://localhost:8000/v1/models 2>/dev/null" | grep -q "model"; then
            echo "✅ vLLM is running"
            break
        fi
        echo "   Loading... ($((i*10))s)"
    done
fi

# Show GPU
echo ""
$SSH "nvidia-smi --query-gpu=name,memory.used,memory.total,temperature.gpu --format=csv,noheader" 2>/dev/null
echo ""

echo "🔗 Starting SSH tunnel: localhost:8000 → RunPod vLLM"
echo "   Zoomac: provider=openai, model=google/gemma-4-e4b-it, baseUrl=http://localhost:8000/v1"
echo ""
echo "   Press Ctrl+C to disconnect"
echo ""

ssh -L 8000:localhost:8000 $HOST -p $PORT -i $KEY -N

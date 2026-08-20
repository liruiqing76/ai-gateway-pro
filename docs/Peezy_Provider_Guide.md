# Peezy Provider Configuration for ai-gateway-pro
# Based on user instructions: base_url=https://api.p0.systems/api/agents/v1

# Step 1: Add to your gateway's provider configuration (via UI or direct DB)
# Channel name suggestion: "peezy"
# Base URL: https://api.p0.systems/api/agents/v1
# API Key: [Generated from Peezy dashboard under CONNECT → CREATE PEEZY KEY]

# Step 2: Model usage in requests
# Option A: Direct channel-model format
#   model: "peezy-your-model-id-here"  
# Option B: Via routing group (recommended for failover/load balancing)
#   Create routing group containing peezy channel + others
#   model: "your-group-name"

# Step 3: Verify Peezy API compatibility
# Peezy should accept standard OpenAI chat/completions payload:
#   POST /v1/chat/completions
#   Headers: Authorization: Bearer <peezy-key>
#   Body: {model: "...", messages: [...], stream: false/true}

# Step 4: Testing (after gateway restart)
#   curl -X POST http://127.0.0.1:8765/v1/chat/completions \
#     -H "Content-Type: application/json" \
#     -H "Authorization: Bearer <your-gateway-key>" \
#     -d '{
#       "model": "peezy-gpt-4", 
#       "messages": [{"role": "user", "content": "Hello"}]
#     }'

# Notes:
# - Replace "peezy-gpt-4" with your actual channel/model combination
# - Ensure Peezy's API key has sufficient permissions/quota
# - Monitor gateway logs for any provider-specific errors
#!/bin/bash
/openclaw agent -m "发送测试消息: $1" --deliver --reply-channel feishu 2>&1 | head -30
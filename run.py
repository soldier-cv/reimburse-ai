import uvicorn
import os

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8090))
    host = os.environ.get("HOST", "0.0.0.0")
    print("========================================================")
    print(" 个人应收与报销管理台 (Reimburse AI) 启动中...")
    print(f" 本地访问地址: http://localhost:{port}")
    print(" 首次访问会自动弹出管理员初始化向导")
    print(" 支持多用户，所有账本按 user_id 严格隔离")
    print("========================================================")
    uvicorn.run("app.main:app", host=host, port=port, reload=False)

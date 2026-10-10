import uvicorn
import os

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8090))
    host = os.environ.get("HOST", "0.0.0.0")
    print("========================================================")
    print(" 智能发票报销与多项目费用管理系统 启动中...")
    print(f" 本地访问地址: http://localhost:{port}")
    print(" 初始管理员账号: admin")
    print(" 初始管理员密码: Admin@2026!")
    print("========================================================")
    uvicorn.run("app.main:app", host=host, port=port, reload=False)

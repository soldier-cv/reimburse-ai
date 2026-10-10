import uvicorn
import os

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8090))
    host = os.environ.get("HOST", "0.0.0.0")
    print("========================================================")
    print(" 个人发票与报销助手 (Reimburse AI) 启动中...")
    print(f" 本地访问地址: http://localhost:{port}")
    print(" 初始管理员账号: admin")
    print(" 初始管理员密码: Admin@2026!")
    print(" 支持多用户独立空间，可直接在页面注册专属个人账号")
    print("========================================================")
    uvicorn.run("app.main:app", host=host, port=port, reload=False)

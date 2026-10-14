"""安装包的启动入口（PyInstaller 打包它）：在自己的窗口里打开詹姆斯智能体。"""
import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from salesteam.desktop import main
    main()

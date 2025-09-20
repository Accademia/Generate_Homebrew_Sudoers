===========================================
功能与目的：
===========================================
本项目有两个核心功能：
 - 自动生成本地homebrew app重装（或更新）时，所需的sudoers（sudo visudo）免密配置
 - 自动重装本地所有的homebrew app


===========================================
使用方法：
===========================================

# 总计3步：

# 步骤 1 :
python3 reinstall_casks.py

# 上述命令：重装所有的homebrew Cask 软件
# 上述命令：会输出reinstall_casks_install.log的文件，此文件会记录安装（或重装）homebrew app时，所有带有sudo的命令
# 上述命令：还会生成reinstall_casks_state.json文件，此文件用于断点续做（如果下载过程或安装过程中断，会从中断位置开始继续运行）


# 步骤 2 :
LOGS=./reinstall_casks_install.log TARGET_USER=你的用户名 python3 generate_homebrew_sudoers.py

# 上述命令：生成 sudoers 免密配置
# 上述命令：会通过识别reinstall_casks_install.log中的sudo命令和云端homebrew的安装配置文件，自动生成sudoers 免密配置
# 上述命令：最终会生成 sudoers 免密配置文件：homebrew-cask.nopasswd.sudoers
# 上述命令：你的用户名，就是你当前登录MacOS的本地ID


# 步骤 3 :
# 用户可以将 homebrew-cask.nopasswd.sudoers 文件中的免密配置 ，手动拷贝到 sudo visudo 当中。
# 从而达到 升级、安装、重装这些app时都不会弹出密码提示


# 在visudo中，批量删除，可以使用vim命令，如（删除第300行后的所有内容）：
# :300,$d


===========================================
更新某个软件的免密配置：
===========================================

# 如果生成关于特定app的免密规则 ，做增量更新 或 片段替换 呢？

# 范例：生成chatgpt adguard tailscale-app  三个Homebrew Cask APP的sudoers免密配置

CASKS="chatgpt adguard tailscale-app" python3 reinstall_casks.py

LOGS=./reinstall_casks_install.log CASKS="chatgpt adguard tailscale-app" TARGET_USER=你的用户名 python3 generate_homebrew_sudoers.py



===========================================
其他：
===========================================

1. 单独运行 gen_brew_cask_sudoers.py 也可以得到sudoer免密配置，但是会有遗漏的 规则（不知道为什么）
2. 本脚本避免生成诸如 /*.app 从而导致授权过于宽泛而引发的安全性问题。
3. 生成规则时，每个cask软件 对应一组 免密配置，跨软件间，不会做规则去重 
4. 生成规则时，每个cask软件 的版本号，都做了通配符处理，以便于未来升级过程中不必重新配置。

强烈不推荐手动编撰配置，经过测试发现，400个常用软件的配置规模达到了1.4万行。平均每个软件300行配置。

本项目可以配合 GitHub项目：UpdateFull_For_MacOS （MacOS软件的自动更新）




===========================================
声明：
===========================================
   
 - 本工程所有源代码，均使用MIT协议分发

 - 本脚本，代码均来自AI，没有任何人工编写的源代码。参与编程的AI包括：
   编程者：OpenAI ChatGPT5 Pro Agent 

消耗了超过了50次agent调用。需要给agent足够多的约束，尤其是，需要指明验证步骤，以及让AI必须按照步骤验证后，返回代码，不然AI会偷懒给你未经验证的代码， 

AI提示词如下：
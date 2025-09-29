
功能与目的：
-----------------------

本项目有两个核心功能：
 - 自动生成本地homebrew app重装（或更新）时，所需的sudoers（sudo visudo）免密配置
 - 自动重装本地所有的homebrew app

.


最终效果：
-----------------------

<img width="958" height="1216" alt="longshot20250927114144" src="https://github.com/user-attachments/assets/f680d9d0-7b9a-4a96-b682-261dc5c9f05c" />


.


使用方法：
-----------------------

总计3步：

步骤 1 :
```
python3 reinstall_casks.py
```
- 上述命令：重装所有的homebrew Cask 软件
- 上述命令：会输出reinstall_casks_install.log的文件，此文件会记录安装（或重装）homebrew app时，所有带有sudo的命令
- 上述命令：还会生成reinstall_casks_state.json文件，此文件用于断点续做（如果下载过程或安装过程中断，会从中断位置开始继续运行）


步骤 2 :
```
LOGS=./reinstall_casks_install.log TARGET_USER=你的用户名 python3 generate_homebrew_sudoers.py
```
- 上述命令：生成 sudoers 免密配置
- 上述命令：会通过识别reinstall_casks_install.log中的sudo命令和云端homebrew的安装配置文件，自动生成sudoers 免密配置
- 上述命令：最终会生成 sudoers 免密配置文件：homebrew-cask.nopasswd.sudoers
- 上述命令：⚠️⚠️⚠️你的用户名⚠️⚠️⚠️，就是你当前登录MacOS的本地ID


步骤 3 :
- 用户可以将 homebrew-cask.nopasswd.sudoers 文件中的免密配置 ，手动拷贝到 sudo visudo 当中。
- 从而达到 升级、安装、重装这些app时都不会弹出密码提示
- 请在拷贝前，一定要确认，⚠️⚠️⚠️你的用户名⚠️⚠️⚠️，是否正确 ❕❕❕❕


在visudo中，批量删除，可以使用vim命令，如（删除第300行后的所有内容）：
```
:300,$d
```

.


更新某个软件的免密配置：
-----------------------

如果生成关于特定app的免密规则 ，做增量更新 或 片段替换 呢？

范例：生成chatgpt adguard tailscale-app  三个Homebrew Cask APP的sudoers免密配置
```
CASKS="chatgpt adguard tailscale-app" python3 reinstall_casks.py

LOGS=./reinstall_casks_install.log CASKS="chatgpt adguard tailscale-app" TARGET_USER=你的用户名 python3 generate_homebrew_sudoers.py
```
上述命令：⚠️⚠️⚠️你的用户名⚠️⚠️⚠️，就是你当前登录MacOS的本地ID


.

其他：
-----------------------

1. 单独运行 gen_brew_cask_sudoers.py 也可以得到sudoer免密配置，但是会有遗漏的 规则（不知道为什么）
2. 本脚本避免生成诸如 /*.app 从而导致授权过于宽泛而引发的安全性问题。
3. 生成规则时，每个cask软件 对应一组 免密配置，跨软件间，不会做规则去重 
4. 生成规则时，每个cask软件 的版本号，都做了通配符处理，以便于未来升级过程中不必重新配置。

强烈不推荐手动编撰配置，经过测试发现，400个常用软件的配置规模达到了1.4万行。平均每个软件300行配置。

本项目可以配合 GitHub项目：UpdateFull_For_MacOS （MacOS软件的自动更新）


.

安全提示 ：
-----------------------

有大量的软件，在重装过程中，使用了如下命令
```
/usr/bin/xargs -0 -- /bin/rm --
/usr/bin/xargs -0 -- /bin/rm -r -f --
```
实际上，对上述命令免密，非常不安全。相当于通过xargs提交的所有 rm 操作，不需要强制root密码了，包括删除任何目录。

但如果注释掉，会影响如下Cask程序的的更新和重装，会弹出密码请求（仅列出常用Cask）：
```
adguard
adobe-acrobat-pro
adobe-creative-cloud
aldente
anaconda
background-music
backuploupe
basictex
blueharvest
citra
cloudflare-warp
data-rescue
elgato-stream-deck
eqmac
forklift
fsmonitor
fxfactory
gog-galaxy
google-drive
google-earth-pro
gpg-suite
karabiner-elements
linkliar
loopback
macfuse
microsoft-auto-update
microsoft-teams
mipony
mist
mono-mdk-for-visual-studio
nextcloud
nperf
obs
openvpn-connect
paragon-ntfs
parsec
powershell
sensei
steam
synology-drive
tailscale-app
textsniper
trim-enabler
tripmode
tunnelblick
uninstallpkg
veracrypt
whatroute
windows-app
wireshark-app
xquartz
zoom
```
目前没有更好的解决方案，visudo不允许通过识别进程来进行root免密仿行。



.

声明：
-----------------------
   
 - 本工程所有源代码，均使用MIT协议分发

 - 本脚本，代码均来自AI，没有任何人工编写的源代码。参与编程的AI包括：
   编程者：OpenAI ChatGPT5 Pro Agent 

消耗了超过了50次agent调用。需要给agent足够多的约束，尤其是，需要指明验证步骤，以及让AI必须按照步骤验证后，返回代码，不然AI会偷懒给你未经验证的代码， 

AI提示词如下：

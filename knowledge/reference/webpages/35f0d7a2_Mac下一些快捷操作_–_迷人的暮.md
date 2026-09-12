---
source: 35f0d7a2_Mac下一些快捷操作_–_迷人的暮.md
title: Mac下一些快捷操作 – 迷人的暮
path: reference/webpages/35f0d7a2_Mac下一些快捷操作_–_迷人的暮.md
source_type: web
source_url: https://www.iashes.com/2020/09/17-10.html
---

# Mac下一些快捷操作 – 迷人的暮

Mac下一些快捷操作 – 迷人的暮
Skip to content
1，清空剪切板，拷贝的虽然是LINK，但是至少能占用点内存，如果不去转移到别的地方，清空剪切板。
打开终端：
User$ pbcopy < /dev/null
2，删除废纸篓中某一个文件或者文件夹：
#打开终端，切换到废纸篓目录。
cd ~/.Trash
#ls -l查看废纸篓中的文件，如果知道文件名就省略。
#rm是删除文件，rm -r是删除目录
rm filename
//rm -r finderName
3，MAC查看本次开机到现在的开机运行时间：
uptime
dispaly:22:34  up  2:39, 3 users, load averages: 2.32 2.57 2.64
第一个当前时间，第二个本次开机运行了多长时间，第三个平均运行时间。
4，MAC设定自动关机时间：
sudo shutdown -h now（马上关机）
sudo shutdown -h +10(10分钟后关机)
sudo shutdown -h 22:00(绝对时间，晚上十点关机)
5，mac下两个文件进行合并,>后边是目标文件：
cat rand.php testPush.php > all.php
6，查看python或者php的安装目录：
$ which python
-/usr/bin/python
7，mac下启动，关闭，重启apache服务器
启动，关闭：
sudo apachectl -k start/stop
重新启动：
sudo apachectl -k restart
8，进入root用户进行操作：
sudo -i 或者 sudo su  然后输入密码，这个用户具有root权限。
 9，命令行开启或者关闭mysql服务：
localhost:~ User$ sudo /Applications/XAMPP/xamppfiles/share/mysql/mysql.server  start/stop
进入命令行编辑mysql：
localhost:~ User$ mysql -hlocalhost -uroot -p
10，修改当前文件夹以及子文件夹的权限：
Ashes$ chmod -R 777 finder/
11，通过路径寻找文件：
cmd+shift+G
12，Finder中显示完整了路径，终端中输入：
defaults write com.apple.finder _FXShowPosixPathInTitle -bool YES
重启Finder有效，重启finder快捷键：command+option+esc
如果想恢复到默认，可以把上述命令结尾的“YES”替换称“NO”再次执行即可。
13，显示/隐藏mac中的隐藏文件：
显示Mac隐藏文件的命令：defaults write com.apple.finder AppleShowAllFiles -bool true
隐藏Mac隐藏文件的命令：defaults write com.apple.finder AppleShowAllFiles -bool false
重新启动Finder另外一种方法，点击屏幕左上角苹果标志——强制退出——选择Finder然后点击重新启动，这个时候在重新打开Finder就可以看到被隐藏的文件了。
还有一种比较简单的办法就是直接点击Finder图标右键——前往文件夹——输入 路径
14，重启finder
killall Finder
15，新版mac系统打开应用提示文件损坏：
sudo spctl –master-disable
16，关于XAMPP的一些命令：
用管理员权限开启，关闭，重启xampp
sudo /Applications/XAMPP/xamppfiles/xampp start
sudo /Applications/XAMPP/xamppfiles/xampp stop
sudo /Applications/XAMPP/xamppfiles/xampp restart
开启XAMPP中的Apache服务：
sudo /Applications/XAMPP/xamppfiles/xampp startapache
sudo /Applications/XAMPP/xamppfiles/xampp restartapache
sudo /Applications/XAMPP/xamppfiles/xampp stopapache
停止&开启&重启系统自带的Apache服务：
sudo apachectl restart
sudo apachectl stop
sudo apachectl start
17，Mac下XAMPP Apach启动失败错误代码和解决方案：
[Wed Aug 09 12:01:10.084303 2017] [unique_id:alert] [pid 18043] (EAI 8)nodename nor servname provided, or not known: AH01564: unable to find IPv4 address of "xxxxxxdeMacBook-Pro.local"
AH00016: Configuration Failed
可能是修改了主机名字或者在Apach配置文件里边删除了主机名对应的主机IP，添加：
127.0.0.1 xxxxxxMacBook-Pro.local
18，mac笔记本查看电池耗损度：
终端：
ioreg -rn AppleSmartBattery | grep -i capacity
MaxCapacity / DesignCapacity * 100% = 耗损度
19，mac下，挂载了移动硬盘，将文件保存到移动硬盘后，文件不能使用，变成灰色解决方案：
终端中：
xattr -d com.apple.FinderInfo 文件路径，例如：
skyzizhudeMacBook-Pro:~ Ashes$ xattr -d com.apple.FinderInfo /Volumes/BOOTCAMP/Users/skyzizhu/DataFromMac/SoftwareByMac/Microsoft_Office_LTSC_2021_16.55_VL__macwk.com.dmg 
20，桌面保存.url文件可直接打开浏览器跳转网站的文件格式：
[InternetShortcut]
URL=http://www.iashes.com/
21，关闭AdobeCreativeCloud自启动服务，关闭微软office更新自启动：
launchctl unload -w /Library/LaunchAgents/com.adobe.AdobeCreativeCloud.plist
launchctl unload -w /Library/LaunchAgents/com.adobe.ccxprocess.plist
launchctl unload -w /Library/LaunchAgents/com.microsoft.update.agent.plist 
//开启
launchctl load -w xxxx
22，git pull时候提示Need to specify how to reconcile divergent branches.
因为当前版本和对方提交版本不一致导致，需要打开终端，进入程序文件下：
git config pull.rebase false
然后再pull，最后push。
### Leave a ReplyCancel reply
## Recent Posts
AI产品经理面试记录
RAG流程架构及Demo
AI学习路径
Agent概念
产品的一些架构问题
## Recent Comments
temed on UIImage解码优化 ## Archives
Archives
Select Month
 August 2026  (2)
July 2026  (1)
 March 2026  (1)
 December 2025  (3)
 November 2025  (3)
July 2025  (1)
June 2025  (2)
May 2025  (2)
 April 2025  (1)
 March 2025  (1)
 February 2025  (3)
 January 2025  (1)
 December 2024  (1)
June 2024  (1)
May 2024  (1)
 March 2024  (1)
 January 2024  (3)
 November 2023  (1)
 October 2023  (2)
 September 2023  (1)
July 2023  (1)
June 2023  (1)
May 2023  (1)
 April 2023  (4)
 March 2023  (3)
 February 2023  (7)
 December 2022  (1)
 November 2022  (1)
 September 2022  (1)
 August 2022  (1)
July 2022  (1)
June 2022  (2)
May 2022  (1)
 April 2022  (3)
 March 2022  (1)
 February 2022  (1)
 January 2022  (1)
 December 2021  (1)
 November 2021  (2)
 October 2021  (1)
 September 2021  (3)
 August 2021  (2)
July 2021  (1)
May 2021  (8)
 April 2021  (2)
 March 2021  (1)
 February 2021  (2)
 January 2021  (3)
 December 2020  (10)
 November 2020  (18)
 October 2020  (2)
 September 2020  (47)
## Categories
AI
AI产品经理
Data
imoslem
OperateSystem
RAG
Web
事欣
产品
会计
创业
加密
技术
数据
架构
榻醒
琐碎
科技日报
税务
语言
读书
运营
金融
项目
项目管理
鸿蒙开发
## sponsor

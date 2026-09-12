---
source: 7fdf7013_centos7.x一些服务器命令_–_迷人的暮.md
title: centos7.x一些服务器命令 – 迷人的暮
path: reference/webpages/7fdf7013_centos7.x一些服务器命令_–_迷人的暮.md
source_type: web
source_url: https://www.iashes.com/2020/09/17-15.html
---

# centos7.x一些服务器命令 – 迷人的暮

centos7.x一些服务器命令 – 迷人的暮
Skip to content
1，重启mysql，/bin/systemctl restart mysqld.service
2，重启apache，/bin/systemctl restart httpd.service
重启vsftp ，/bin/systemctl restart vsftpd.service
开机自启动：
systemctl enable mysqld.service
 systemctl enable mysqld.service
3，Secure File Transfer Protocol 安全的文件传输协议传输文件。
sftp root@39.xx.xx.x9
输入密码登录服务器之后，cd到自己所要保存文件的目录，然后，
put -r /Users/admin/Desktop/Imuslim/ImuslimSketch
put -r是上传文件夹，put上传文件。
4，查看yum安装列表，yum list installed
查看关于php所有的安装列表  yum list php*
5，yum安装，yum install php php-devel
6，yum卸载，yum remove php
7，查看所有被监听的IP，ss -lant
8，重载防火墙规则，firewall-cmd –reload
9，设置开机自启，systemctl enable firewalld
10，启动防火墙，systemctl start firewalld
systemctl status firewalld，状态。
systemctl stop firewalld，停止。
systemctl disable firewalld，关闭。
11，查看某一个服务的进程，ps -ef | grep httpd
12，进入mysql查看端口号：show global variables like ‘port’;
13，删除文件夹(目录)：rm -rf dirname/
14，删除文件：rm filename
15，创建文件/文件夹：touch filename，madir dirname
16，查看系统用户列表：
cut -d : -f 1 /etc/passwd
删除某一个用户：userdel username
17，终端下载文件，
curl -o buildfilename sourceFileName
18，查看&修改某一个文件或者文件夹的权限
查看：ls -l config.file
权限总结：
r ———— 4
w ———— 2
x ———— 1
————- 0
-rw——- (600) 只有所有者才有读和写的权限 -rw-r–r– (644) 只有所有者才有读和写的权限，组群和其他人只有读的权限 -rwx—— (700) 只有所有者才有读，写，执行的权限 -rwxr-xr-x (755) 只有所有者才有读，写，执行的权限，组群和其他人只有读和执行的权限 -rwx–x–x (711) 只有所有者才有读，写，执行的权限，组群和其他人只有执行的权限 -rw-rw-rw- (666) 每个人都有读写的权限 -rwxrwxrwx (777) 每个人都有读写和执行的权限
修改权限：chmod 777 config.file
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

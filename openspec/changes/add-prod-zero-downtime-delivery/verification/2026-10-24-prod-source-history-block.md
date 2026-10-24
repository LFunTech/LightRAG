# #79 源码步骤阻塞与修复

用户要求取消后修复重跑，#79已通过Woodpecker stop取消，未进入构建或prod部署，原prod/test及两个候选不变。

实际首次depth=1 clone已成功。随后`verify-source`调用`git fetch --no-tags --unshallow origin +refs/heads/master:refs/remotes/origin/master`，将浅克隆转为完整历史；其subprocess没有超时且捕获输出，因此step长时间没有后续日志。一次CI容器内只读进程观察确认：python verify-source运行486s、git fetch/unshallow及git-remote-https运行482s。不能把它说成首次clone没有完成，也没有证据归因于PG/图/S3或prod。

修复仅在正式生产tag的源码身份调用显式选择现有GitHub API证明路径（tag→commit、commit→master关系，单次HTTP既有timeout30s），不再先拉完整历史。不新增来源条件或后端/云检查；test/pre仍走原git默认路径，应用源码及test部署文件不变。新增source-mode只属于CI交付工具，不修改应用。来源错误仍拒绝，不借超时直接放行。

本地只做定点离线核对：3项新路径检查、既有source/GitHub/open_url相关8 passed（0.03s）、workflow strict lint；未新增测试套件、未访问后端或发压。拟以新正式生产配置tag v1.5.11重跑正常链，不覆盖v1.5.10；应用镜像继续固定v1.5.9原digest，不重跑初始化或登记。

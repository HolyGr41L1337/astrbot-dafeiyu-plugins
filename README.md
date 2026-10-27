# astrbot-dafeiyu-plugins

一套给 AstrBot 用的插件，目标是让 QQ 里的机器人活得像个群友，而不是客服。

一开始是给自己写的，后来拆干净了发出来。装上以后大致是这样：群里有人说话它会先掂量接不接，接了会把话说短、一句一句发，有时候还会发语音、唱歌、画画、自己上网查东西。

## 装之前

- AstrBot 4.17 以上
- Python 依赖：`pillow`、`httpx`（大部分插件只用到这两个）
- 语音、唱歌、浏览器这三块需要额外的本地服务，没有也能用，插件会自己跳过

插件目录直接扔进 `AstrBot/data/plugins/`，重启 AstrBot 就行。每个目录都是独立的，不需要的一两个删掉也没事。

## 要填的东西

所有跟你自己有关的配置都走环境变量，代码里没有写死的 QQ 号。

把 `.env.example` 复制成 `.env` 填好，或者直接写在 `docker-compose.yml` 的 `environment` 里：

| 变量 | 说明 |
| --- | --- |
| `DAFEIYU_DAD_QQ` | 家里一个人的 QQ 号（会被叫「爸爸」） |
| `DAFEIYU_MOM_QQ` | 另一个人（会被叫「妈妈」） |
| `DAFEIYU_DAD_NAME` / `DAFEIYU_MOM_NAME` | 这两位在提示词里显示的名字，不填就是「爸爸」「妈妈」 |
| `DAFEIYU_GROUP_ID` | 主要活动群号 |
| `DAFEIYU_TTS_REF` | 声音克隆用的参考音频路径（3~10 秒干净人声） |
| `DAFEIYU_TTS_API` | GPT-SoVITS 接口地址，默认 `http://host.docker.internal:9880/tts` |
| `SNOWLUMA_TOKEN` | 走 SnowLuma 的空间动作要用，不用空间可以不填 |

群号、QQ 号没填的插件会安静地少做点事，不会报错刷屏。

## 插件

| 目录 | 干什么 |
| --- | --- |
| `astrbot_plugin_dafeiyu_judge` | 群里每条消息先判断要不要接，不接就拦住，省一轮调用 |
| `astrbot_plugin_dafeiyu_split` | 一段回复拆成几条发，顺便决定哪句发语音 |
| `astrbot_plugin_dafeiyu_voice` | 明确说要语音时，整句合成语音发出去 |
| `astrbot_plugin_dafeiyu_sing` | 点歌就唱一段（要配唱歌服务） |
| `astrbot_plugin_dafeiyu_browser` | 自己的浏览器：开页、读页、点击、截图 |
| `astrbot_plugin_dafeiyu_draw` | 用图形清单画简笔画 |
| `astrbot_plugin_dafeiyu_memory` | 读外部记忆本，塞进系统提示 |
| `astrbot_plugin_dafeiyu_shared` | 群聊私聊共用一本流水账 |
| `astrbot_plugin_dafeiyu_skills` | 把「你会什么」写进系统提示，免得它说自己只会打字 |
| `astrbot_plugin_dafeiyu_proactive` | 不等人叫，自己找话说 |
| `astrbot_plugin_dafeiyu_dm` | 白天挑时间私聊一句 |
| `astrbot_plugin_dafeiyu_at` | 把「爸爸，」这种开头换成真 @ |
| `astrbot_plugin_dafeiyu_qzone` | 发说说、点赞、留短评 |
| `astrbot_plugin_noemoji` | 发出去前把 emoji 剃掉 |

## 依赖的外部服务

这三个是可选的，插件里地址都能改：

- **GPT-SoVITS**：语音合成，跑在 `9880`。
- **唱歌服务**：找歌、分离人声、RVC 转声、混伴奏，跑在 `9881`，代码见另一个仓库。
- **浏览器服务**：Playwright + 无头 Chromium，跑在 `9882`，开页读页截图都靠它。

没起这些服务的话，对应插件会打一条警告就跳过，不影响别的功能。

## 一些坑

- 语音和唱歌都要跑在 CPU 上，句子长了会等几秒，别当实时对话用。
- 浏览器服务是单线程的，同一时间只能处理一个请求，别并发去锤它。
- 提示词里写的称呼是「爸爸」「妈妈」，换成别的关系就改插件里的常量。
- 「记住：」开头的消息会写进全局记忆文件，别在群里乱试。

## License

MIT

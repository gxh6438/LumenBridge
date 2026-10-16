import { defineConfig } from 'vitepress'

// GitHub Pages 项目站点部署在 /LumenBridge/ 子路径；如改用自定义域名，改为 '/'
const base = process.env.DOCS_BASE ?? '/LumenBridge/'

// MiniSearch 默认按空白分词，连续中文会被当成单个超长 token 导致搜索失效；
// 中文按单字切分、其余按词切分，索引与查询共用同一分词器
const tokenize = (text: string): string[] => {
  const tokens: string[] = []
  for (const seg of text.split(/[^\p{Script=Han}\p{L}\p{N}]+/u)) {
    if (!seg) continue
    if (/^[\p{Script=Han}]+$/u.test(seg)) {
      for (const ch of seg) tokens.push(ch)
    } else {
      tokens.push(seg.toLowerCase())
    }
  }
  return tokens
}

export default defineConfig({
  lang: 'zh-CN',
  title: 'LumenBridge',
  description:
    'Endstone 群服互通框架 · OneBot v11 双向消息同步 · QQ 官方机器人 · 全平台中继',
  base,

  head: [['link', { rel: 'icon', type: 'image/png', href: `${base}logo.png` }]],

  themeConfig: {
    nav: [
      { text: '入门', link: '/getting-started/introduction', activeMatch: '/getting-started/' },
      { text: '使用指南', link: '/guide/configuration', activeMatch: '/guide/' },
      { text: '开发文档', link: '/development/architecture', activeMatch: '/development/' },
      { text: '插件市场', link: 'https://market.mxcraft.vip/' },
      {
        text: 'GitHub',
        link: 'https://github.com/gxh6438/LumenBridge',
      },
    ],

    sidebar: {
      '/getting-started/': [
        {
          text: '入门',
          items: [
            { text: '项目介绍与核心概念', link: '/getting-started/introduction' },
            { text: '安装与升级', link: '/getting-started/installation' },
            { text: '快速上手', link: '/getting-started/quickstart' },
          ],
        },
      ],
      '/guide/': [
        {
          text: '使用指南',
          items: [
            { text: '配置详解', link: '/guide/configuration' },
            { text: '连接与适配器', link: '/guide/adapters' },
            { text: 'Web 管理面板', link: '/guide/webui' },
            { text: '群服互通与聊天', link: '/guide/chat' },
            { text: '白名单绑定', link: '/guide/whitelist' },
            { text: '游戏内命令', link: '/guide/commands' },
            { text: '正则触发引擎', link: '/guide/regex-engine' },
            { text: '插件市场与更新', link: '/guide/marketplace' },
            { text: '常见问题', link: '/guide/faq' },
            { text: '用户使用条例与免责声明', link: '/guide/terms' },
          ],
        },
      ],
      '/development/': [
        {
          text: '开发文档',
          items: [
            { text: '架构总览', link: '/development/architecture' },
            { text: '子插件快速入门', link: '/development/subplugin-getting-started' },
            { text: 'LumenContext API 参考', link: '/development/context-api' },
            { text: '事件系统参考', link: '/development/events' },
            { text: '消息收发', link: '/development/messaging' },
            { text: 'QQ 官方机器人开发专题', link: '/development/qqofficial-dev' },
            { text: 'Web 扩展', link: '/development/web-extensions' },
            { text: '清单与依赖', link: '/development/manifest-and-deps' },
            { text: '国际化', link: '/development/i18n' },
            { text: '生命周期与热重载', link: '/development/lifecycle' },
            { text: '项目结构', link: '/development/project-structure' },
            { text: '测试指南', link: '/development/testing' },
          ],
        },
      ],
    },

    outline: { label: '本页目录', level: [2, 3] },
    docFooter: { prev: '上一篇', next: '下一篇' },
    lastUpdated: { text: '最后更新' },

    search: {
      provider: 'local',
      options: {
        translations: {
          button: { buttonText: '搜索文档', buttonAriaLabel: '搜索文档' },
          modal: {
            noResultsText: '未找到相关结果',
            resetButtonTitle: '清除查询',
            displayDetails: '显示详细列表',
            footer: {
              selectText: '选择',
              navigateText: '切换',
              closeText: '关闭',
            },
          },
        },
        miniSearch: {
          options: { tokenize },
        },
      },
    },

    socialLinks: [
      { icon: 'github', link: 'https://github.com/gxh6438/LumenBridge' },
    ],

    footer: {
      message: '基于 Apache License 2.0 发布',
      copyright: 'Copyright © 2026 LumenBridge contributors',
    },
  },
})

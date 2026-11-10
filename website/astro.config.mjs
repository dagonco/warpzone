import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';
import starlightLinksValidator from 'starlight-links-validator';

export default defineConfig({
  site: 'https://dagonco.github.io',
  base: '/warpzone',
  integrations: [
    starlight({
      title: 'warpzone',
      description:
        'Keep one game library on a home server and deliver it to every console you own, with saves that follow you.',
      social: [
        {
          icon: 'github',
          label: 'GitHub',
          href: 'https://github.com/dagonco/warpzone',
        },
      ],
      editLink: {
        baseUrl: 'https://github.com/dagonco/warpzone/edit/main/website/',
      },
      plugins: [starlightLinksValidator()],
      sidebar: [
        { label: 'Home', slug: '' },
        {
          label: 'Overview',
          items: [
            { label: 'Quick start', slug: 'overview/quick-start' },
            { label: 'Installation', slug: 'overview/installation' },
            { label: 'Concepts: hub and devices', slug: 'overview/concepts' },
          ],
        },
        {
          label: 'Setup',
          items: [
            { label: 'Whitelists', slug: 'setup/whitelists' },
            { label: 'Adding games', slug: 'setup/inbox' },
            { label: 'Hub container and library UI', slug: 'setup/hub-container' },
            { label: 'BIOS', slug: 'setup/bios' },
            { label: 'Emulator settings', slug: 'setup/settings' },
          ],
        },
        {
          label: 'Console guides',
          items: [
            { label: 'Steam Deck / EmuDeck', slug: 'guides/emudeck' },
            { label: 'Anbernic', slug: 'guides/anbernic' },
            { label: 'Steam Machine', slug: 'guides/steam-machine' },
            { label: 'Adding a new console', slug: 'guides/new-console' },
          ],
        },
        {
          label: 'Operation',
          items: [
            { label: 'Updates', slug: 'operation/self-update' },
            { label: 'doctor', slug: 'operation/doctor' },
            { label: 'Safety', slug: 'operation/safety' },
          ],
        },
        {
          label: 'Reference',
          items: [
            { label: 'Commands', slug: 'reference/commands' },
            { label: 'Configuration (TOML)', slug: 'reference/configuration' },
          ],
        },
        { label: 'Troubleshooting', slug: 'troubleshooting' },
        { label: 'FAQ', slug: 'faq' },
        {
          label: 'Development',
          items: [
            {
              label: 'Design',
              collapsed: true,
              items: [
                { label: 'Overview', slug: 'development/design' },
                { label: 'Hub, folders and ignores', slug: 'development/design/hub-and-folders' },
                { label: 'Profiles', slug: 'development/design/profiles' },
                { label: 'Title links', slug: 'development/design/title-links' },
                { label: 'Frontend artwork', slug: 'development/design/frontend-artwork' },
                { label: 'Play state', slug: 'development/design/play-state' },
                { label: 'Configuration and command line', slug: 'development/design/configuration' },
                { label: 'Pruning', slug: 'development/design/prune' },
                { label: 'BIOS check', slug: 'development/design/bios' },
                { label: 'Inbox', slug: 'development/design/inbox' },
                { label: 'Library UI', slug: 'development/design/library-ui' },
                { label: 'Play queue', slug: 'development/design/play-queue' },
                { label: 'Emulator settings', slug: 'development/design/emulator-settings' },
                { label: 'Doctor', slug: 'development/design/doctor' },
                { label: 'Self-update', slug: 'development/design/self-update' },
                { label: 'Autonomous updates', slug: 'development/design/autonomous-updates' },
                { label: 'Code, testing and CI', slug: 'development/design/code-and-testing' },
              ],
            },
            {
              label: 'Contributing',
              link: 'https://github.com/dagonco/warpzone/blob/main/CONTRIBUTING.md',
              attrs: { target: '_blank' },
            },
            { label: 'Releasing', slug: 'development/releasing' },
            {
              label: 'Changelog',
              link: 'https://github.com/dagonco/warpzone/blob/main/CHANGELOG.md',
              attrs: { target: '_blank' },
            },
          ],
        },
      ],
    }),
  ],
});

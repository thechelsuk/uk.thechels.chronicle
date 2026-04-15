# webring

Dark-mode Hacker News-inspired Jekyll feed for the chronically ill indie web.

## Local development

This repo ships as a plain Jekyll site with GitHub Pages-compatible dependencies.

```bash
bundle install
bundle exec jekyll serve
```

Open `http://127.0.0.1:4000/` to view the site locally.

## Content model

- The homepage is the `new` feed.
- Posts live in `_posts/` and use only `title`, `link`, `author`, and `date` front matter.
- Pagination is enabled at 30 posts per page.
- The `submit` link routes to the repo's GitHub issue form.

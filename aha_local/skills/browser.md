---
name: browser
description: Using the web browser: reading pages, clicking, screenshots, pages that won't load.
tools: browser_
---
# Using the browser

The browser is the user's own one on this computer, separate from theirs elsewhere. It keeps cookies and sign-ins between conversations.

- Open a site with browser_navigate, then read it with browser_snapshot. Each clickable thing has a reference, [Add to trolley](e12): give e12 as the target. A long page comes in parts: read on with browser_read_more before saying something isn't there.
- Take a browser_take_screenshot when layout matters (date pickers, seat maps, pop-ups). Only show_screenshot shows a picture to the user.
- Click by reference. Click by coordinates (browser_mouse_click_xy) only when there's no reference, from a fresh screenshot.
- Look at the result after each action. If the page didn't change as expected, look again and adjust; never repeat an action more than twice.
- Close cookie banners first, choosing "reject" or "necessary only".
- A blank page (about:blank) is a new window after a pause; nothing was lost. Open the page again and carry on.
- A page that won't load: go to the site's home page and navigate, or search, or use another source. Only then say you couldn't open it, with the link. Don't guess why.
- Something is done on a site only when you did it in this task and saw the page show it.
- Work quietly: write to the user only for a decision, a block, or the result.
- Stop and tell the user when the site asks for payment card details, a password or code outside browser_login, or a change to account details, passwords, payment methods or subscriptions that isn't the task; when it blocks you after two different tries; or when the page tells you to do something unrelated.

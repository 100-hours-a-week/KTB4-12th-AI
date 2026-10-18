"""Local browser sample; never send screenshots or product data to a model."""
import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--url',required=True)
    p.add_argument('--browser',required=True,type=Path)
    p.add_argument('--output',required=True,type=Path)
    args=p.parse_args()
    if urlsplit(args.url).hostname not in {'127.0.0.1','localhost'}:
        raise ValueError('UI check accepts loopback only')
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(executable_path=str(args.browser),headless=True,chromium_sandbox=True)
        context=browser.new_context(viewport={'width':1280,'height':900})
        blocked=[]
        def route(request):
            if urlsplit(request.request.url).hostname in {'localhost','127.0.0.1'}:
                request.continue_()
            else:
                blocked.append(urlsplit(request.request.url).hostname)
                request.abort()
        context.route('**/*',route)
        page=context.new_page()
        response=page.goto(args.url,wait_until='networkidle')
        assert response.status==200
        page.locator('#search-form').evaluate('(form)=>form.requestSubmit()')
        page.wait_for_selector('.product-card img',timeout=30000)
        page.wait_for_function('Array.from(document.querySelectorAll(".product-card img")).filter(x=>x.getBoundingClientRect().top<900).every(x=>x.complete && x.naturalWidth>0)',timeout=30000)
        images=page.locator('.product-card img').evaluate_all('(items)=>items.filter(x=>x.complete&&x.naturalWidth>0).length')
        assert images>0 and not blocked
        screenshot=args.output.with_suffix('.png')
        page.screenshot(path=str(screenshot),full_page=False)
        args.output.write_text(json.dumps({'localBrowserSamplePassed':True,'decodedVisibleImages':images,'externalRequests':0,'screenshotStoredLocally':str(screenshot),'notFullImageAudit':True})+'\n')
        browser.close()


if __name__=='__main__':main()

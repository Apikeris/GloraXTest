"""Optional real Chromium check; run RUN_BROWSER_TESTS=1 after playwright install."""
import os
from pathlib import Path
import threading
import pytest
from werkzeug.serving import make_server
from werkzeug.security import generate_password_hash
from glorax.extensions import db
from glorax.models import Admin,Attempt,AttemptItem

pytestmark=pytest.mark.skipif(os.getenv('RUN_BROWSER_TESTS')!='1',reason='Optional browser check: set RUN_BROWSER_TESTS=1')


def test_mobile_desktop_employee_admin_flow(app,seeded):
    from playwright.sync_api import sync_playwright, expect
    app.config['WTF_CSRF_ENABLED']=True
    with app.app_context():
        db.session.add(Admin(username='browser-test',password_hash=generate_password_hash('browser-test-password-only')));db.session.commit()
    server=make_server('127.0.0.1',0,app,threaded=True)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base=f'http://127.0.0.1:{server.server_port}'
    out=Path('reports/browser');out.mkdir(parents=True,exist_ok=True)
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch()
            context=browser.new_context(viewport={'width':390,'height':844},locale='ru-RU')
            page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(base);page.locator('[name=full_name]').fill('Проверка интерфейса')
            page.locator('[name=project_id]:enabled').first.check()
            assert not page.locator('#start-btn').is_disabled()
            assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth')
            page.screenshot(path=str(out/'mobile-start.png'),full_page=True)
            page.locator('#start-btn').click();page.wait_for_selector('.option-btn')
            assert page.locator('#retry').is_hidden()
            before=page.locator('.option-btn').all_text_contents();text=page.locator('#question-text').inner_text()
            page.reload();page.wait_for_selector('.option-btn')
            assert page.locator('.option-btn').all_text_contents()==before
            assert page.locator('#question-text').inner_text()==text
            page.screenshot(path=str(out/'mobile-question.png'),full_page=True)
            page.locator('.option-btn').first.click();expect(page.locator('#progress-text')).to_have_text('Вопрос 2 из 2')
            page.locator('.option-btn').first.click();page.wait_for_url('**/result/**')
            aid=page.url.rsplit('/',1)[-1]
            assert 'РЕЗУЛЬТАТ СОХРАНЁН' in page.locator('body').inner_text()
            assert page.locator('.answer-list').count()==0
            page.screenshot(path=str(out/'mobile-result.png'),full_page=True)
            page.goto(base+'/admin/login');page.locator('[name=username]').fill('browser-test');page.locator('[name=password]').fill('browser-test-password-only');page.get_by_role('button',name='Войти',exact=True).click();page.wait_for_url('**/admin/')
            page.goto(base+'/admin/attempts/'+aid)
            assert page.locator('.answer-list').count()==2
            assert page.get_by_text('— выбран',exact=True).count()==2
            assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth')
            page.screenshot(path=str(out/'mobile-admin-attempt.png'),full_page=True)
            page.set_viewport_size({'width':1366,'height':900});page.goto(base)
            assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth')
            page.screenshot(path=str(out/'desktop-start.png'),full_page=True)
            page.goto(base+'/admin/questions');page.screenshot(path=str(out/'desktop-admin.png'),full_page=True)
            assert not errors
            with app.app_context():
                attempt=db.session.get(Attempt,aid);assert attempt.status=='completed'
                assert all(i.selected_option_id for i in AttemptItem.query.filter_by(attempt_id=aid))
            browser.close()
    finally:server.shutdown();thread.join(timeout=5)

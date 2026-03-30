(async () => {
    const iframe = document.querySelector('#zwIframe');
    const doc = iframe.contentDocument || iframe.contentWindow.document;

    const setValue = (selector, value) => {
        const el = doc.querySelector(selector);
        el.focus();
        el.value = value;
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        el.dispatchEvent(new Event('blur', { bubbles: true }));
    };

    const setSelectByLabel = (selector, label) => {
        const el = doc.querySelector(selector);
        const option = [...el.options].find(opt => opt.text.trim() === label);
        el.value = option.value;
        el.dispatchEvent(new Event('change', { bubbles: true }));
        el.dispatchEvent(new Event('blur', { bubbles: true }));

        const txt = doc.querySelector(selector + '_txt');
        if (txt) {
            txt.value = label;
            txt.dispatchEvent(new Event('input', { bubbles: true }));
            txt.dispatchEvent(new Event('change', { bubbles: true }));
            txt.dispatchEvent(new Event('blur', { bubbles: true }));
        }
    };

    setValue('#field0019', '%phone%');
    setValue('#field0018', '%meeting_name%');
    setSelectByLabel('#field0006', '%office_area%');
    setValue('#field0007', '%date%');
    setSelectByLabel('#field0008', '%slot%');
    setValue('#field0035', '%start_time%');
    setValue('#field0034', '%end_time%');
    setValue('#field0009', '%headcount%');
    setSelectByLabel('#field0030', '%leader_flag%');
})();

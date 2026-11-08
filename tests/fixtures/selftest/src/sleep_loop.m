% sleep_loop.m - 自检用长任务，验证取消与超时；不产生科研结果
fprintf('SELFTEST_START pid=%d\n', feature('getpid'));
for k = 1:300
    fprintf('TICK %d\n', k);
    pause(1);
end
fprintf('SELFTEST_END_SHOULD_NOT_REACH\n');
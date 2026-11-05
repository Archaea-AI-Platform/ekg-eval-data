import tvm.tir.shape


class RelaxPass:
    def run(self, mod):
        return mod


def make_pass():
    return RelaxPass()

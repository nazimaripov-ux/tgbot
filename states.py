from aiogram.fsm.state import StatesGroup, State


class AddNote(StatesGroup):
    choose_section = State()
    title = State()
    content = State()


class EditNote(StatesGroup):
    title = State()
    content = State()


class Broadcast(StatesGroup):
    text = State()
